import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from grok import executable
from processes import identity, popen_options
from storage import ACTIVE, TERMINAL, Store


def fingerprint(path):
    stat = path.stat()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path), "size": stat.st_size, "sha256": digest}


def files_under(path):
    if not path.exists():
        return []
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError(f"Refusing a linked directory: {path}")
    files = []
    for child in sorted(path.iterdir()):
        if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
            raise ValueError(f"Refusing a linked path: {child}")
        files.extend(files_under(child) if child.is_dir() else [fingerprint(child)])
    return files


def native_dir(session_id):
    import uuid
    uuid.UUID(session_id)
    home = Path(os.environ.get("GROK_HOME") or Path.home() / ".grok")
    matches = list((home / "sessions").glob(f"*/{session_id}"))
    if len(matches) > 1:
        raise ValueError(f"Ambiguous native session path: {session_id}")
    return matches[0] if matches else None


def plan(store, ids=None, days=7):
    all_tasks = store.list()
    if ids is not None:
        chosen = [store.get(i) for i in ids]
    else:
        chosen = [t for t in all_tasks if t["status"] in TERMINAL and t["delivered"] and not t["keep"]
                  and t["updated"] < time.time() - days * 86400]
    roots = {t["session_id"] for t in chosen if t["session_id"]}
    chosen_ids = {t["id"] for t in chosen}
    # A native conversation is shared by its successive MCP turns.
    chosen = [t for t in all_tasks if t["session_id"] in roots or t["id"] in chosen_ids]
    for task in chosen:
        if task["status"] in ACTIVE or task["status"] == "cleaned" or not task["delivered"] or task["keep"]:
            raise ValueError(f"Task is active, unread, retained, or already cleaned: {task['id']}")
        if not ids and task["updated"] >= time.time() - days * 86400:
            raise ValueError(f"Session was used recently: {task['session_id']}; use explicit task IDs after review")
    native = set(roots)
    child_sessions = set()
    for task in chosen:
        for child in task["children"]:
            if child.get("child_session_id"):
                native.add(child["child_session_id"])
                child_sessions.add(child["child_session_id"])
    outside = [t for t in all_tasks if t["id"] not in {x["id"] for x in chosen} and t["status"] != "cleaned"]
    if any(t["session_id"] in native or any(c.get("child_session_id") in native for c in t["children"]) for t in outside):
        raise ValueError("A native session is also referenced by another task")
    directories = [store.path(t["id"]) for t in chosen]
    native_paths = [path for sid in sorted(native) if (path := native_dir(sid)) is not None]
    for task in chosen:
        if any(Path(p).resolve().is_relative_to(root.resolve()) for p in task.get("deliverables", []) for root in directories):
            raise ValueError("A final deliverable is inside a task directory; move it out before cleaning that task")
    files = []
    for path in directories + native_paths:
        boundary = store.root if path in directories else Path(os.environ.get("GROK_HOME") or Path.home() / ".grok") / "sessions"
        if not path.resolve().is_relative_to(boundary.resolve()):
            raise ValueError(f"Directory resolves outside its owned storage: {path}")
        files.extend(files_under(path))
    selected = {t["id"] for t in chosen}
    log_files = []
    for log in sorted({t["log"] for t in all_tasks if t.get("log")}):
        path = Path(log)
        if not path.exists():
            continue
        refs = [t for t in all_tasks if t.get("log") == log]
        if any((t["id"] not in selected and t["status"] != "cleaned") or t["keep"] or
               identity(t["pid"]) == t["birth"] for t in refs):
            continue
        if not any(t["id"] in selected for t in refs) and path.stat().st_mtime >= time.time() - days * 86400:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to((store.root / "logs").resolve()):
            raise ValueError(f"Log is outside the registered log directory: {path}")
        log_files.append(str(path))
        files.append(fingerprint(path))
    result = {"client": store.client, "root": str(store.root.resolve()), "task_ids": sorted(t["id"] for t in chosen),
              "native_sessions": sorted(native), "task_directories": [str(p) for p in directories],
              "child_sessions": sorted(child_sessions),
              "log_files": log_files, "days": days,
              "native_directories": [str(p) for p in native_paths], "files": files,
              "preserved_artifacts": sorted({p for t in chosen for p in t["artifacts"]
                                              if not any(Path(p).resolve().is_relative_to(root.resolve()) for root in directories)}),
              "task_versions": {t["id"]: t["updated"] for t in chosen}}
    result["confirmation"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()[:16]
    return result


def delete_listed_directory(path, records):
    expected = [f for f in records if Path(f["path"]).is_relative_to(path)]
    if files_under(path) != expected:
        raise ValueError(f"Directory changed during cleanup: {path}")
    for record in expected:
        target = Path(record["path"])
        if fingerprint(target) != record:
            raise ValueError(f"File changed during cleanup: {target}")
        target.unlink()
    for directory in sorted((p for p in path.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        directory.rmdir()
    path.rmdir()


def apply_plan(store, saved, confirmation):
    if confirmation != saved["confirmation"]:
        raise ValueError("Confirmation does not match the reviewed plan")
    fresh = plan(store, saved["task_ids"], saved["days"])
    if fresh != saved:
        raise ValueError("Tasks or files changed since preview; create and review a new plan")
    tasks = [store.get(i) for i in saved["task_ids"]]
    locked = set()
    with store.lock, store.db:
        store.db.execute("BEGIN IMMEDIATE")
        for task in tasks:
            current = store.get(task["id"])
            if current["status"] in ACTIVE or current["updated"] != saved["task_versions"][task["id"]]:
                raise ValueError("Task changed during cleanup preparation")
            sid = task["session_id"]
            if sid and sid not in locked:
                if store.db.execute("SELECT 1 FROM tasks WHERE session_id=? AND status IN ('starting','running','cancelling','cleaning')", (sid,)).fetchone():
                    raise ValueError("Session is active")
                locked.add(sid)
            owned = dict(task, cleanup_pid=store.pid, cleanup_birth=store.birth)
            store.db.execute("UPDATE tasks SET status='cleaning',data=? WHERE id=?", (json.dumps(owned), task["id"]))
    try:
        for sid in saved["native_sessions"]:
            result = subprocess.run([executable(), "sessions", "delete", sid], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, encoding="utf-8", timeout=60, **popen_options())
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            remaining = native_dir(sid)
            if remaining is not None:
                summary = json.loads((remaining / "summary.json").read_text(encoding="utf-8"))
                if sid not in saved["child_sessions"] or summary.get("info", {}).get("id") != sid or summary.get("session_kind") not in ("subagent", "subagent_fork"):
                    raise RuntimeError(f"Grok left the native session directory in place: {sid}")
                # The CLI's local lookup excludes hidden subagent sessions; remote deletion/index eviction still runs.
                delete_listed_directory(remaining, saved["files"])
        task_paths = [Path(p) for p in saved["task_directories"]]
        for root in task_paths:
            delete_listed_directory(root, saved["files"])
        for log in saved["log_files"]:
            record = next(f for f in saved["files"] if f["path"] == log)
            if fingerprint(Path(log)) != record:
                raise ValueError(f"Log changed during cleanup: {log}")
            Path(log).unlink()
        with store.lock, store.db:
            for task in tasks:
                task.update(prompt="[history cleaned]", scope="", activity="History cleaned", error=None)
                task["children"] = [{k: c[k] for k in ("subagent_id", "child_session_id", "parent_session_id", "status") if k in c}
                                    for c in task["children"]]
                store.db.execute("UPDATE tasks SET status='cleaned',data=? WHERE id=?", (json.dumps(task), task["id"]))
            store.db.executemany("DELETE FROM sessions WHERE id=?", [(sid,) for sid in locked])
    except Exception as exc:
        with store.lock, store.db:
            for task in tasks:
                task["error"] = f"Cleanup incomplete: {exc}"
                store.db.execute("UPDATE tasks SET status='cleanup_failed',data=? WHERE id=?", (json.dumps(task), task["id"]))
            store.db.executemany("DELETE FROM sessions WHERE id=?", [(sid,) for sid in locked])
        raise


def main():
    parser = argparse.ArgumentParser(description="Inspect Grok-MCP tasks and preview scoped history cleanup")
    parser.add_argument("--client", choices=("codex", "claude"), required=True)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("list")
    inspect = subs.add_parser("inspect")
    inspect.add_argument("id")
    keep = subs.add_parser("keep")
    keep.add_argument("id")
    keep.add_argument("--off", action="store_true")
    preview = subs.add_parser("cleanup-plan")
    preview.add_argument("--ids", nargs="+")
    preview.add_argument("--sessions", nargs="+", help="Select every task of these native Grok sessions")
    preview.add_argument("--days", type=int, default=7)
    preview.add_argument("--out", type=Path, required=True)
    clean = subs.add_parser("cleanup")
    clean.add_argument("plan", type=Path)
    clean.add_argument("--confirm", required=True)
    args = parser.parse_args()
    store = Store(args.client)
    try:
        store.recover()
        if args.command == "list":
            for task in store.list():
                print(json.dumps({k: task.get(k) for k in ("id", "owner", "status", "session_id", "delivered", "keep", "updated")}, ensure_ascii=False))
        elif args.command == "inspect":
            print(json.dumps(store.get(args.id), ensure_ascii=False, indent=2))
        elif args.command == "keep":
            store.keep(args.id, not args.off)
        elif args.command == "cleanup-plan":
            ids = args.ids
            if args.sessions:
                known = {t["session_id"] for t in store.list()}
                missing = [sid for sid in args.sessions if sid not in known]
                if missing:
                    raise ValueError("No registered task uses these sessions: " + ", ".join(missing))
                ids = (ids or []) + [t["id"] for t in store.list() if t["session_id"] in args.sessions]
            result = plan(store, ids, args.days)
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            apply_plan(store, json.loads(args.plan.read_text(encoding="utf-8")), args.confirm)
            print("Cleanup completed; project artifacts were preserved")
    finally:
        store.close()


if __name__ == "__main__":
    main()
