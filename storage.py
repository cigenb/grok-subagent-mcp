import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from processes import identity

CLIENTS = ("codex", "claude")
ACTIVE = ("starting", "running", "cancelling", "cleaning")
TERMINAL = ("completed", "failed", "interrupted")


def data_root():
    return Path(os.environ.get("GROK_MCP_DATA_DIR") or Path.home() / ".grok" / "cigenb-grok-mcp")


class Store:
    def __init__(self, client, root=None):
        if client not in CLIENTS:
            raise ValueError("client must be codex or claude")
        self.client = client
        self.root = (Path(root) if root else data_root()) / client
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "tasks.sqlite3", timeout=15, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, session_id TEXT,
                status TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                updated REAL NOT NULL, data TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS active_session ON tasks(session_id)
                WHERE status IN ('starting','running','cancelling');
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, cwd TEXT NOT NULL
            );
        """)
        self.pid = os.getpid()
        self.birth = identity(self.pid)

    def close(self):
        self.db.close()

    def path(self, task_id):
        uuid.UUID(task_id)
        return self.root / "tasks" / task_id

    @staticmethod
    def decode(row):
        if row is None:
            raise ValueError("Task not found")
        task = json.loads(row["data"])
        task.update(status=row["status"], delivered=bool(row["delivered"]), updated=row["updated"])
        return task

    def get(self, task_id, owner=None):
        with self.lock:
            row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            task = self.decode(row)
            if owner is not None and task["owner"] != owner:
                raise ValueError("Task belongs to a different source conversation")
            return task

    def list(self, owner=None):
        with self.lock:
            rows = self.db.execute("SELECT * FROM tasks" + (" WHERE owner=?" if owner else "") +
                                   " ORDER BY updated DESC", (owner,) if owner else ()).fetchall()
            return [self.decode(row) for row in rows]

    def create(self, owner, prompt, scope, cwd, session_id=None):
        task = dict(id=str(uuid.uuid4()), owner=owner, client=self.client, prompt=prompt, scope=scope,
                    cwd=cwd, session_id=session_id, status="starting", created=time.time(),
                    pid=self.pid, birth=self.birth, model="grok-4.7", effort="high", children=[],
                    artifacts=[], activity="Starting Grok", error=None, keep=False)
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if session_id and not self.db.execute("SELECT 1 FROM sessions WHERE id=? AND owner=?", (session_id, owner)).fetchone():
                raise ValueError("Session was removed or belongs to another source conversation")
            if session_id and self.db.execute("SELECT 1 FROM tasks WHERE session_id=? AND status='cleaning'", (session_id,)).fetchone():
                raise ValueError("This Grok session is being cleaned")
            try:
                self.db.execute("INSERT INTO tasks(id,owner,session_id,status,updated,data) VALUES(?,?,?,?,?,?)",
                                (task["id"], owner, session_id, task["status"], time.time(), json.dumps(task)))
            except sqlite3.IntegrityError as exc:
                raise ValueError("This Grok session already has an active task") from exc
            self.path(task["id"]).mkdir(parents=True)
            (self.path(task["id"]) / "scratch").mkdir()
        return task

    def save(self, task):
        with self.lock, self.db:
            self.db.execute("UPDATE tasks SET session_id=?,status=?,updated=?,data=? WHERE id=?",
                            (task["session_id"], task["status"], time.time(), json.dumps(task), task["id"]))

    def bind(self, task, session_id):
        with self.lock, self.db:
            self.db.execute("INSERT INTO sessions(id,owner,cwd) VALUES(?,?,?)",
                            (session_id, task["owner"], task["cwd"]))
            task["session_id"] = session_id
            self.db.execute("UPDATE tasks SET session_id=?,data=? WHERE id=?",
                            (session_id, json.dumps(task), task["id"]))

    def session(self, session_id, owner):
        with self.lock:
            row = self.db.execute("SELECT * FROM sessions WHERE id=? AND owner=?", (session_id, owner)).fetchone()
            if row is None:
                raise ValueError("Only Grok sessions registered to this source conversation can be resumed")
            return dict(row)

    def mark_delivered(self, ids):
        with self.lock, self.db:
            self.db.executemany("UPDATE tasks SET delivered=1 WHERE id=? AND status IN ('completed','failed','interrupted')",
                                [(i,) for i in ids])

    def keep(self, task_id, enabled):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            task = self.get(task_id)
            if task["status"] in ACTIVE or task["status"] == "cleaned":
                raise ValueError("Wait for the task to finish before changing retention")
            task["keep"] = enabled
            self.db.execute("UPDATE tasks SET data=?,updated=? WHERE id=?", (json.dumps(task), time.time(), task_id))

    def recover(self):
        for task in self.list():
            if task["status"] == "cleaning" and identity(task["cleanup_pid"]) != task["cleanup_birth"]:
                task.update(status="cleanup_failed", error="Cleanup process exited; review a fresh cleanup plan before retrying")
                with self.lock, self.db:
                    self.db.execute("UPDATE tasks SET status=?,updated=?,data=? WHERE id=?",
                                    (task["status"], time.time(), json.dumps(task), task["id"]))
                    self.db.execute("DELETE FROM sessions WHERE id=?", (task["session_id"],))
            elif task["status"] in ("starting", "running", "cancelling") and identity(task["pid"]) != task["birth"]:
                task.update(status="interrupted", error="Owning MCP process exited; saved output is still available")
                self.save(task)

    def output(self, task, full=False):
        path = self.path(task["id"]) / "output.txt"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if not full and len(text) > 6000:
            text = text[:6000] + f"\n[truncated; use grok_status(full=true), or read {path}]"
        return text

    def format(self, task, full=False):
        text = (f"[{task['id']}] {task['status']} | session {task['session_id'] or '-'}\n"
                f"cwd: {task['cwd']}\nactivity: {task['activity']}\n")
        if task["error"]:
            text += f"error: {task['error']}\n"
        if task.get("warning"):
            text += f"warning: {task['warning']}\n"
        text += self.output(task, full)
        return text
