"""Local chat UI with server-owned credentials and independent conversation histories."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage

from .config import WebSettings

ASSETS = Path(__file__).parent / "static"
COOKIE = "simagent_session"
INSTRUCTIONS = """
你是 simAgent 的问答助手，默认用中文清晰回答用户的问题。
当前是普通模型问答模式，没有注册任何工具，也没有读取用户项目、文件或数据库。
可以解释概念、讨论建模思路和分析用户在对话中提供的数据。
不要声称已经执行运筹算法、机器学习模型或仿真；解释结论时说明依据和假设。
用户需要实际调用工具时，说明该能力还需要接入工具并使用框架的执行流程。
"""


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=32000)

    @field_validator("message", mode="before")
    @classmethod
    def strip_message(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


@dataclass
class ChatSession:
    history: list[ModelMessage] = field(default_factory=list)
    messages: list[dict[str, str]] = field(default_factory=list)
    busy: bool = False
    task: asyncio.Task[None] | None = None
    touched: float = field(default_factory=time.monotonic)


def error_message(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "回答超时，请重试，或在 .env 中增大 SIMAGENT_TIMEOUT_SECONDS 后重启。"
    code = getattr(error, "status_code", None)
    messages = {
        400: "请求被模型接口拒绝。请检查模型配置，或新建对话缩短上下文。",
        401: "API Key 无效，请修改 .env 中的 DEEPSEEK_API_KEY 并重启。",
        402: "DeepSeek 账户余额不足，请检查账户。",
        403: "当前账户没有访问模型的权限。",
        404: "模型或接口不存在，请检查 .env 中的模型名称和接口地址。",
        429: "请求过于频繁或已达到配额，请稍后重试。",
    }
    # Do not return raw upstream errors, headers, URLs or request bodies to the browser.
    return messages.get(code, "模型请求失败，请确认网络和 .env 配置，稍后重试。")


def create_app(settings: WebSettings | None = None) -> FastAPI:
    settings = settings or WebSettings.from_env()
    sessions: dict[str, ChatSession] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from openai import AsyncOpenAI
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.deepseek import DeepSeekProvider

        app.state.agent = None
        if not settings.configured:
            yield
            return
        async with AsyncOpenAI(
            api_key=settings.api_key.get_secret_value().strip(),
            base_url=str(settings.base_url), timeout=settings.timeout_seconds, max_retries=0,
        ) as client:
            model = OpenAIChatModel(settings.model, provider=DeepSeekProvider(openai_client=client))
            app.state.agent = Agent(
                model, output_type=str, name="simagent_web_chat", instructions=INSTRUCTIONS,
            )
            try:
                yield
            finally:
                pending = [session.task for session in sessions.values() if session.task]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    app = FastAPI(title="simAgent", lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_browser_policy(request: Request, call_next):
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

    def get_session(request: Request) -> ChatSession:
        session = sessions.get(request.cookies.get(COOKIE, ""))
        if session is None:
            raise HTTPException(409, "会话已过期，请刷新页面。")
        session.touched = time.monotonic()
        return session

    def new_session(response: Response) -> ChatSession:
        expired = [key for key, value in sessions.items()
                   if not value.busy and time.monotonic() - value.touched > 8 * 3600]
        for key in expired:
            sessions.pop(key)
        if len(sessions) >= 128:
            raise HTTPException(503, "会话数量已满，请稍后再试。")
        key = secrets.token_urlsafe(32)
        session = ChatSession()
        sessions[key] = session
        response.set_cookie(COOKIE, key, httponly=True, samesite="strict", path="/")
        return session

    def session_view(session: ChatSession) -> dict:
        return {"model": settings.model, "configured": settings.configured,
                "messages": session.messages, "busy": session.busy}

    @app.get("/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/api/session")
    async def read_session(request: Request, response: Response):
        session = sessions.get(request.cookies.get(COOKIE, "")) or new_session(response)
        session.touched = time.monotonic()
        return session_view(session)

    @app.post("/api/session/reset")
    async def reset_session(request: Request):
        session = get_session(request)
        if session.busy:
            raise HTTPException(409, "回答仍在生成，请停止后再新建对话。")
        session.history.clear()
        session.messages.clear()
        return session_view(session)

    @app.post("/api/chat")
    async def chat(body: ChatInput, request: Request):
        session = get_session(request)
        if app.state.agent is None:
            raise HTTPException(503, "请在 .env 中填写 DEEPSEEK_API_KEY，然后重启服务。")
        if session.busy:
            raise HTTPException(409, "当前会话正在生成回答，请稍候。")
        if len(session.messages) >= 200:
            raise HTTPException(409, "当前对话已达到 100 轮，请新建对话。")
        session.busy = True
        queue: asyncio.Queue[dict] = asyncio.Queue()

        def event(kind: str, **data) -> str:
            return json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n"

        async def produce():
            chunks: list[str] = []
            try:
                async with asyncio.timeout(settings.timeout_seconds):
                    async with app.state.agent.run_stream(
                        body.message, message_history=session.history,
                    ) as result:
                        async for text in result.stream_text(delta=True):
                            chunks.append(text)
                            queue.put_nowait({"type": "delta", "text": text})
                        history = result.all_messages()
                answer = "".join(chunks)
                if not answer.strip():
                    queue.put_nowait({"type": "error", "message": "模型没有返回文本，请重试。"})
                    return
                session.history = history
                session.messages.extend([
                    {"role": "user", "content": body.message},
                    {"role": "assistant", "content": answer},
                ])
                queue.put_nowait({"type": "done"})
            except asyncio.CancelledError:
                raise
            except Exception as error:
                queue.put_nowait({"type": "error", "message": error_message(error)})

        task = asyncio.create_task(produce())
        session.task = task

        def completed(future: asyncio.Task[None]) -> None:
            if future.cancelled():
                queue.put_nowait({"type": "stopped"})
            elif future.exception() is not None:
                queue.put_nowait({"type": "error", "message": "生成失败，请重试。"})
            if session.task is future:
                session.busy = False
                session.task = None
                session.touched = time.monotonic()

        task.add_done_callback(completed)

        async def watch_disconnect():
            while not task.done():
                if await request.is_disconnected():
                    task.cancel()
                    return
                await asyncio.sleep(0.5)

        async def stream():
            watcher = asyncio.create_task(watch_disconnect())
            try:
                yield event("start")
                while True:
                    item = await queue.get()
                    yield json.dumps(item, ensure_ascii=False) + "\n"
                    if item["type"] in {"done", "error", "stopped"}:
                        break
            finally:
                watcher.cancel()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, watcher, return_exceptions=True)

        return StreamingResponse(stream(), media_type="application/x-ndjson",
                                 headers={"X-Accel-Buffering": "no"})

    @app.post("/api/chat/cancel")
    async def cancel_chat(request: Request):
        session = get_session(request)
        task = session.task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return {"stopped": True}

    app.mount("/static", StaticFiles(directory=ASSETS), name="static")
    return app
