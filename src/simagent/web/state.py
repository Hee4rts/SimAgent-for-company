"""Browser sessions backed by the framework's local SQLite storage."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from ..infrastructure.storage import SQLiteRunStore
from ..tools.leave_approval.task import ApprovalWorkspace


@dataclass
class TaskThread:
    id: str = field(default_factory=lambda: uuid4().hex)
    mode: str = "general"
    title: str = "通用任务"
    title_custom: bool = False
    updated_at: float = field(default_factory=time.time)
    deleted_at: float | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    workspace: ApprovalWorkspace = field(default_factory=ApprovalWorkspace)
    runtime: Any = field(default=None, repr=False)
    busy: bool = False
    task: asyncio.Task | None = field(default=None, repr=False)


@dataclass
class ChatSession:
    key: str
    active_id: str | None = None
    threads: dict[str, TaskThread] = field(default_factory=dict)
    touched: float = field(default_factory=time.monotonic)

    @property
    def thread(self):
        return self.threads[self.active_id]

    @property
    def busy(self):
        return any(thread.busy for thread in self.threads.values())

    def create_thread(self, mode):
        thread = TaskThread(mode=mode, title="审批电子流" if mode == "approval" else "通用任务")
        self.threads[thread.id] = thread
        self.active_id = thread.id
        return thread


class WebStorage(SQLiteRunStore):
    def __init__(self, path=".simagent/runs.sqlite3"):
        super().__init__(path)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS web_sessions (
                    id TEXT PRIMARY KEY, document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_workspaces (
                    id TEXT PRIMARY KEY, document TEXT NOT NULL
                );
            """)

    def attach(self, workspace):
        workspace.on_change = lambda: self.save_workspace(workspace)
        self.save_workspace(workspace)

    def save_workspace(self, workspace):
        document = json.dumps(workspace.dump(), ensure_ascii=False, allow_nan=False)
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO web_workspaces VALUES (?, ?)", (workspace.version, document))

    @staticmethod
    def _session_document(session, exclude_thread_id=None):
        return {"active_id": session.active_id, "threads": {
            key: {"id": thread.id, "mode": thread.mode, "title": thread.title,
                  "title_custom": thread.title_custom, "deleted_at": thread.deleted_at,
                  "updated_at": thread.updated_at, "messages": thread.messages, "run_ids": thread.run_ids,
                  "workspace_id": thread.workspace.version}
            for key, thread in session.threads.items() if key != exclude_thread_id
        }}

    def save_session(self, session):
        document = self._session_document(session)
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO web_sessions VALUES (?, ?)",
                       (session.key, json.dumps(document, ensure_ascii=False, allow_nan=False)))

    def purge_thread(self, session, thread):
        """Remove a trashed thread and its private records in one transaction."""
        if session.threads.get(thread.id) is not thread or thread.deleted_at is None or thread.busy:
            raise ValueError("Only an inactive thread in recently deleted can be permanently removed")
        document = self._session_document(session, exclude_thread_id=thread.id)
        # A stopped synchronous tool may still finish later. Detach persistence
        # under the same lock used by its workspace writes to prevent resurrection.
        with thread.workspace.lock:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                remaining_threads = list(document["threads"].values())
                for row in db.execute("SELECT document FROM web_sessions WHERE id != ?", (session.key,)):
                    remaining_threads.extend(json.loads(row[0])["threads"].values())
                used_runs = {run_id for saved in remaining_threads for run_id in saved.get("run_ids", [])}
                workspace_used = any(saved.get("workspace_id") == thread.workspace.version
                                     for saved in remaining_threads)
                result_ids = set()
                for run_id in set(thread.run_ids) - used_runs:
                    row = db.execute("SELECT document FROM runs WHERE id=?", (run_id,)).fetchone()
                    if row:
                        run = json.loads(row[0])
                        result_ids.update(task["result_ref"] for task in run.get("tasks", {}).values()
                                          if task.get("result_ref"))
                    db.execute("DELETE FROM events WHERE run_id=?", (run_id,))
                    db.execute("DELETE FROM runs WHERE id=?", (run_id,))
                # A result referenced by another run must remain available to it.
                if result_ids:
                    for row in db.execute("SELECT document FROM runs"):
                        result_ids.difference_update(task.get("result_ref")
                                                     for task in json.loads(row[0]).get("tasks", {}).values())
                    db.executemany("DELETE FROM results WHERE id=?", ((reference,) for reference in result_ids))
                if not workspace_used:
                    db.execute("DELETE FROM web_workspaces WHERE id=?", (thread.workspace.version,))
                db.execute("INSERT OR REPLACE INTO web_sessions VALUES (?, ?)",
                           (session.key, json.dumps(document, ensure_ascii=False, allow_nan=False)))
            # Update memory only after the transaction commits successfully.
            if not workspace_used:
                thread.workspace.on_change = None
            del session.threads[thread.id]
            thread.runtime = None

    def load_session(self, key):
        if not key:
            return None
        with self._connect() as db:
            row = db.execute("SELECT document FROM web_sessions WHERE id=?", (key,)).fetchone()
            if row is None:
                return None
            document = json.loads(row[0])
            session = ChatSession(key=key, active_id=document.get("active_id"))
            for old_key, saved in document["threads"].items():
                stored = db.execute("SELECT document FROM web_workspaces WHERE id=?", (saved["workspace_id"],)).fetchone()
                workspace = ApprovalWorkspace.restore(json.loads(stored[0])) if stored else ApprovalWorkspace()
                messages = saved["messages"]
                for message in messages:
                    if message.get("status") == "running":
                        message["status"] = "interrupted"
                        message["content"] = "服务曾中断。已保存任务计划和执行记录，可以继续处理；未确认结束的工具任务需要重新处理。"
                        message["stage"] = {"stage": "interrupted", "label": "运行中断"}
                mode = saved.get("mode", old_key)
                thread_id = saved.get("id", uuid4().hex)
                first_question = next((item["content"] for item in messages if item["role"] == "user"), "")
                title = saved.get("title") or " ".join(first_question.split())[:24] or (
                    "审批电子流" if mode == "approval" else "通用任务")
                title_custom = saved.get("title_custom", False)
                if not title_custom and not first_question and title in {"新建通用任务", "新建审批电子流"}:
                    title = title.removeprefix("新建")
                session.threads[thread_id] = TaskThread(id=thread_id, mode=mode, title=title,
                    title_custom=title_custom, updated_at=saved.get("updated_at", time.time()),
                    deleted_at=saved.get("deleted_at"),
                    messages=messages, run_ids=saved["run_ids"], workspace=workspace)
                if "active_id" not in document and old_key == document.get("mode"):
                    session.active_id = thread_id
            available = [thread for thread in session.threads.values() if thread.deleted_at is None]
            if not available:
                session.create_thread("general")
            elif session.active_id not in {thread.id for thread in available}:
                session.active_id = max(available, key=lambda thread: thread.updated_at).id
        for thread in session.threads.values():
            self.attach(thread.workspace)
        self.save_session(session)
        return session
