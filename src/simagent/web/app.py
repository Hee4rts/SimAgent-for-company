"""Web presentation of the project's planning, execution and interpretation framework."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_ai import FunctionToolset, Tool
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset

from ..infrastructure.config import WebSettings
from ..infrastructure.dependencies import Services
from ..tools.catalog import write_tool_catalog
from ..tools.toolset import toolset as default_toolset
from ..tools.leave_approval.task import approval_toolset
from .runtime import FrameworkRuntime, execution_toolset, response_text, run_view
from .state import ChatSession, WebStorage

logger = logging.getLogger(__name__)
ASSETS = Path(__file__).parent / "static"
COOKIE = "simagent_session"


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=32000)
    task_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("message", mode="before")
    @classmethod
    def strip_message(cls, value):
        return value.strip() if isinstance(value, str) else value


class TaskCreation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["general", "approval"]


class TaskSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=64)


class TaskRename(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=80)

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, value):
        return value.strip() if isinstance(value, str) else value


def error_message(error):
    if isinstance(error, TimeoutError):
        return "当前阶段超时，已保存运行记录，可以继续处理。"
    messages = {
        400: "请求被模型接口拒绝，请检查模型配置或缩短任务上下文。",
        401: "API Key 无效，请修改 .env 中的 DEEPSEEK_API_KEY 并重启。",
        402: "模型账户余额不足，请检查账户。", 403: "当前账户没有访问模型的权限。",
        404: "模型或接口不存在，请检查模型名称和接口地址。",
        429: "请求过于频繁或已达到配额，请稍后重试。",
    }
    return messages.get(getattr(error, "status_code", None),
                        "框架处理未完成。已保留计划和运行记录，请检查模型配置、输入条件或工具服务后继续处理。")


def create_app(
    settings: WebSettings | None = None,
    *,
    tools: Sequence[Tool[Services] | Callable[..., Any]] = (),
    toolsets: Sequence[AbstractToolset[Services]] | None = None,
    services: Services | None = None,
    storage: WebStorage | None = None,
) -> FastAPI:
    settings = settings or WebSettings.from_env()
    services = services if services is not None else Services()
    services = replace(services, clients={"serpapi_api_key": settings.serpapi_api_key, **services.clients})
    selected_toolsets = list(toolsets) if toolsets is not None else [default_toolset]
    if tools:
        selected_toolsets.insert(0, FunctionToolset(tools))
    combined = CombinedToolset(selected_toolsets)
    approval_tools = approval_toolset()
    storage = storage if storage is not None else WebStorage()
    sessions: dict[str, ChatSession] = {}

    @asynccontextmanager
    async def lifespan(app):
        try:
            await asyncio.to_thread(write_tool_catalog, default_toolset)
        except OSError:
            logger.warning("无法更新工具清单文件。")
        app.state.model = None
        app.state.execution_tools = None
        if not settings.configured:
            yield
            return
        from openai import AsyncOpenAI
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.deepseek import DeepSeekProvider

        async with AsyncOpenAI(api_key=settings.api_key.get_secret_value().strip(),
                               base_url=str(settings.base_url), timeout=settings.timeout_seconds,
                               max_retries=0) as client, combined:
            model = OpenAIChatModel(settings.model, provider=DeepSeekProvider(openai_client=client))
            app.state.model = model
            app.state.execution_tools = await execution_toolset(selected_toolsets, services, model)
            try:
                yield
            finally:
                pending = [thread.task for session in sessions.values()
                           for thread in session.threads.values() if thread.task]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    app = FastAPI(title="simAgent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.storage = storage
    app.state.toolset = combined
    app.state.approval_toolset = approval_tools

    @app.middleware("http")
    async def local_browser_policy(request, call_next):
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            foreign_origin = origin and urlsplit(origin).netloc != request.headers.get("host")
            if request.headers.get("sec-fetch-site") == "cross-site" or foreign_origin:
                return Response(status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    def find_session(key):
        session = sessions.get(key)
        if session is None:
            session = storage.load_session(key)
            if session is not None:
                sessions[key] = session
        if session:
            session.touched = time.monotonic()
        return session

    def get_session(request):
        session = find_session(request.cookies.get(COOKIE, ""))
        if session is None:
            raise HTTPException(409, "当前任务连接已过期，请刷新页面。")
        return session

    def new_session(response):
        for key, value in list(sessions.items()):
            if not value.busy and time.monotonic() - value.touched > 8 * 3600:
                sessions.pop(key)
        if len(sessions) >= 128:
            raise HTTPException(503, "任务连接数量已满，请稍后再试。")
        session = ChatSession(key=secrets.token_urlsafe(32))
        session.create_thread("general")
        for thread in session.threads.values():
            storage.attach(thread.workspace)
        storage.save_session(session)
        sessions[session.key] = session
        response.set_cookie(COOKIE, session.key, httponly=True, samesite="strict", path="/")
        return session

    def get_thread(session, task_id=None):
        thread = session.threads.get(task_id or session.active_id)
        if thread is None or thread.deleted_at is not None:
            raise HTTPException(404, "没有找到这个任务。")
        return thread

    def task_history(session):
        return {
            "tasks": [{"id": item.id, "mode": item.mode, "title": item.title, "busy": item.busy}
                      for item in sorted(session.threads.values(), key=lambda item: item.updated_at, reverse=True)
                      if item.deleted_at is None],
            "deleted_tasks": [{"id": item.id, "mode": item.mode, "title": item.title,
                               "deleted_at": item.deleted_at}
                              for item in sorted(session.threads.values(),
                                                 key=lambda item: item.deleted_at or 0, reverse=True)
                              if item.deleted_at is not None],
        }

    def session_view(session, thread=None):
        thread = thread or session.thread
        messages = [{key: item[key] for key in ("role", "content", "status") if key in item}
                    for item in thread.messages]
        return {"model": settings.model, "configured": settings.configured,
                "task_id": thread.id, "title": thread.title, "mode": thread.mode,
                "messages": messages, "busy": thread.busy,
                **task_history(session)}

    def get_runtime(thread):
        if app.state.model is None:
            raise HTTPException(503, "请在 .env 中填写 DEEPSEEK_API_KEY，然后重启服务。")
        if thread.runtime is None:
            thread.runtime = FrameworkRuntime(model=app.state.model,
                tools=approval_tools if thread.mode == "approval" else app.state.execution_tools,
                services=services, storage=storage, thread=thread, mode=thread.mode,
                timeout=settings.timeout_seconds)
        return thread.runtime

    @app.get("/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/api/session")
    async def read_session(request: Request, response: Response, task_id: str | None = None):
        session = find_session(request.cookies.get(COOKIE, "")) or new_session(response)
        return session_view(session, get_thread(session, task_id))

    @app.get("/api/tools")
    async def read_tools(task: Literal["general", "approval"] = "general"):
        if task == "approval":
            definitions = [tool.tool_def for tool in approval_tools.tools.values()]
        elif app.state.execution_tools is not None:
            definitions = [tool.tool_def for tool in app.state.execution_tools.tools.values()]
        elif all(isinstance(item, FunctionToolset) for item in selected_toolsets):
            definitions = [tool.tool_def for item in selected_toolsets for tool in item.tools.values()]
        else:
            raise HTTPException(503, "配置模型并重启后才能连接远程工具。")
        return [{"name": item.name, "description": item.description,
                 "parameters_json_schema": item.parameters_json_schema,
                 "return_schema": item.return_schema} for item in definitions]

    @app.post("/api/session/task")
    async def select_task(body: TaskSelection, request: Request):
        session = get_session(request)
        thread = get_thread(session, body.task_id)
        session.active_id = thread.id
        storage.save_session(session)
        return session_view(session, thread)

    @app.post("/api/tasks")
    async def create_task(body: TaskCreation, request: Request):
        session = get_session(request)
        thread = session.create_thread(body.mode)
        storage.attach(thread.workspace)
        storage.save_session(session)
        return session_view(session, thread)

    @app.get("/api/tasks")
    async def read_tasks(request: Request):
        return task_history(get_session(request))

    @app.patch("/api/tasks/{task_id}")
    async def rename_task(task_id: str, body: TaskRename, request: Request):
        session = get_session(request)
        thread = get_thread(session, task_id)
        thread.title = body.title
        thread.title_custom = True
        thread.updated_at = time.time()
        storage.save_session(session)
        return task_history(session)

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str, request: Request):
        session = get_session(request)
        thread = get_thread(session, task_id)
        if thread.busy:
            raise HTTPException(409, "任务正在处理，请先停止任务再删除。")
        thread.deleted_at = time.time()
        available = [item for item in session.threads.values() if item.deleted_at is None]
        if not available:
            replacement = session.create_thread("general")
            storage.attach(replacement.workspace)
        elif session.active_id == thread.id:
            session.active_id = max(available, key=lambda item: item.updated_at).id
        storage.save_session(session)
        return {**task_history(session), "fallback_task_id": session.active_id}

    @app.post("/api/tasks/{task_id}/restore")
    async def restore_task(task_id: str, request: Request):
        session = get_session(request)
        thread = session.threads.get(task_id)
        if thread is None:
            raise HTTPException(404, "没有找到这个任务。")
        if thread.deleted_at is not None:
            thread.deleted_at = None
            thread.updated_at = time.time()
            storage.save_session(session)
        return task_history(session)

    @app.delete("/api/tasks/{task_id}/permanent")
    async def permanently_delete_task(task_id: str, request: Request):
        session = get_session(request)
        thread = session.threads.get(task_id)
        if thread is None:
            raise HTTPException(404, "没有找到这个任务，可能已被彻底删除。")
        if thread.deleted_at is None:
            raise HTTPException(409, "只能彻底删除“最近删除”中的任务，请先将任务移入最近删除。")
        if thread.busy or (thread.task is not None and not thread.task.done()):
            raise HTTPException(409, "任务正在处理，请先停止任务再彻底删除。")
        storage.purge_thread(session, thread)
        return task_history(session)

    @app.post("/api/session/reset")
    async def reset_session(request: Request):
        session = get_session(request)
        thread = session.create_thread(session.thread.mode)
        storage.attach(thread.workspace)
        storage.save_session(session)
        return session_view(session)

    def owned_run(session, run_id):
        if not any(run_id in thread.run_ids for thread in session.threads.values() if thread.deleted_at is None):
            raise HTTPException(404, "当前任务中没有该运行记录。")
        return storage.load(run_id)

    @app.get("/api/runs/{run_id}")
    async def read_run(run_id: str, request: Request):
        run = owned_run(get_session(request), run_id)
        return run_view(run, storage)

    @app.get("/api/runs/{run_id}/events")
    async def read_events(run_id: str, request: Request):
        owned_run(get_session(request), run_id)
        return [event.model_dump(mode="json") for event in storage.events(run_id)]

    def start_turn(session, thread, message, resume_id=None):
        if thread.busy:
            raise HTTPException(409, "当前任务正在处理，请稍候。")
        if len(thread.messages) >= 200:
            raise HTTPException(409, "当前任务已达到 100 轮，请新建任务。")
        runtime = get_runtime(thread)
        thread.busy = True
        thread.updated_at = time.time()
        if not thread.messages and not thread.title_custom:
            thread.title = " ".join(message.split())[:24]
        queue = asyncio.Queue()
        answer = {"role": "assistant", "content": "", "status": "running", "run_ids": []}
        thread.messages.extend([{"role": "user", "content": message}, answer])
        storage.save_session(session)

        def emit(event):
            if event["type"] == "stage":
                answer["stage"] = {key: value for key, value in event.items() if key != "type"}
            elif event["type"] == "run":
                run_id = event["run"]["id"]
                if run_id not in answer["run_ids"]:
                    answer["run_ids"].append(run_id)
            storage.save_session(session)
            queue.put_nowait(event)

        runtime.emit = emit

        async def produce():
            try:
                result = await (runtime.resume(resume_id) if resume_id else runtime.run(message))
                answer["content"] = response_text(result)
                answer["status"] = "needs_input" if result.reply and result.reply.reason == "missing_input" else "completed"
                if not result.reply and result.runs:
                    state = result.runs[-1].state.value
                    if state in {"interrupted", "cancelled", "failed"}:
                        answer["status"] = state
                        emit({"type": "stage", "stage": state,
                              "label": {"interrupted": "运行中断", "cancelled": "已停止", "failed": "任务未完成"}[state]})
                    elif not result.evaluation.passed:
                        emit({"type": "stage", "stage": "completed", "label": "计算完成，部分目标未满足"})
                if runtime.stop_requested:
                    answer["status"] = "stopped"
                    queue.put_nowait({"type": "stopped", "text": answer["content"]})
                else:
                    queue.put_nowait({"type": "done", "text": answer["content"]})
            except asyncio.CancelledError:
                answer["status"] = "stopped" if runtime.stop_requested else "interrupted"
                answer["content"] = "已停止处理，已保存计划与执行记录。" if runtime.stop_requested else "运行中断，已保存计划与执行记录。"
                emit({"type": "stage", "stage": answer["status"], "label": "已停止" if runtime.stop_requested else "运行中断"})
                queue.put_nowait({"type": "stopped", "text": answer["content"]})
                raise
            except Exception as error:
                answer["status"] = "failed"
                answer["content"] = error_message(error)
                emit({"type": "stage", "stage": "failed", "label": "处理未完成"})
                queue.put_nowait({"type": "error", "message": answer["content"]})
            finally:
                runtime.emit = lambda event: None
                thread.busy = False
                thread.task = None
                thread.updated_at = time.time()
                session.touched = time.monotonic()
                storage.save_session(session)

        thread.task = asyncio.create_task(produce())

        async def stream():
            yield json.dumps({"type": "start"}) + "\n"
            while True:
                item = await queue.get()
                yield json.dumps(item, ensure_ascii=False) + "\n"
                if item["type"] in {"done", "error", "stopped"}:
                    break
            # Browser disconnection does not cancel a persisted framework run.

        return StreamingResponse(stream(), media_type="application/x-ndjson",
                                 headers={"X-Accel-Buffering": "no"})

    @app.post("/api/chat")
    async def chat(body: ChatInput, request: Request):
        session = get_session(request)
        return start_turn(session, get_thread(session, body.task_id), body.message)

    @app.post("/api/runs/{run_id}/resume")
    async def resume(run_id: str, request: Request):
        session = get_session(request)
        owned_run(session, run_id)
        thread = next(thread for thread in session.threads.values()
                      if thread.deleted_at is None and run_id in thread.run_ids)
        return start_turn(session, thread, "继续处理已保存的运行记录。", resume_id=run_id)

    @app.post("/api/chat/cancel")
    async def cancel_chat(request: Request, task_id: str | None = None):
        session = get_session(request)
        thread = get_thread(session, task_id)
        task = thread.task
        if task and not task.done():
            await thread.runtime.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return {"stopped": True}

    app.mount("/static", StaticFiles(directory=ASSETS), name="static")
    return app
