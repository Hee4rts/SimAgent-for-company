from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from ..contracts import JobState, JobUpdate, ToolInput, ToolResult, new_id
from ..dependencies import Services
from ..errors import JobNotFoundError, ToolExecutionError
from .base import ModelTool

LocalFunction = Callable[[ToolInput, Services], ToolResult | Awaitable[ToolResult]]


class LocalFunctionTool(ModelTool):
    """Wrap existing Python algorithms. Sync functions execute in worker threads."""

    def __init__(
        self,
        function: LocalFunction,
        *,
        services: Services | None = None,
        **kwargs: Any,
    ) -> None:
        if kwargs.pop("resumable", False):
            raise ValueError("Local in-memory jobs cannot be durable")
        super().__init__(resumable=False, **kwargs)
        self.function = function
        self.services = services or Services()
        self._jobs: dict[str, asyncio.Task[ToolResult]] = {}
        self._keys: dict[str, str] = {}
        self._synchronous = not inspect.iscoroutinefunction(function)

    async def submit(self, value: ToolInput, *, idempotency_key: str) -> str:
        if idempotency_key in self._keys:
            return self._keys[idempotency_key]
        value = self.validate_input(value)
        job_id = new_id()
        self._keys[idempotency_key] = job_id
        task = asyncio.create_task(self._invoke(value), name=f"model-tool:{job_id}")
        # Consume exceptions even if the caller is interrupted before polling.
        task.add_done_callback(lambda job: None if job.cancelled() else job.exception())
        self._jobs[job_id] = task
        return job_id

    async def _invoke(self, value: ToolInput) -> ToolResult:
        if self._synchronous:
            output = await asyncio.to_thread(self.function, value, self.services)
        else:
            output = self.function(value, self.services)
        if inspect.isawaitable(output):
            output = await output
        return self.validate_result(ToolResult.model_validate(output))

    def _job(self, job_id: str) -> asyncio.Task[ToolResult]:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise JobNotFoundError(f"Local job is unavailable: {job_id}") from None

    async def status(self, job_id: str) -> JobUpdate:
        job = self._job(job_id)
        if job.cancelled():
            return JobUpdate(job_id=job_id, state=JobState.CANCELLED)
        if not job.done():
            return JobUpdate(job_id=job_id, state=JobState.RUNNING)
        error = job.exception()
        return JobUpdate(
            job_id=job_id,
            state=JobState.FAILED if error else JobState.SUCCEEDED,
            message=str(error) if error else None,
            retryable=isinstance(error, ToolExecutionError) and error.retryable,
        )

    async def result(self, job_id: str) -> ToolResult:
        return (await asyncio.shield(self._job(job_id))).model_copy(deep=True)

    async def cancel(self, job_id: str) -> bool:
        job = self._job(job_id)
        if job.done():
            return True
        if self._synchronous:
            # A cancelled asyncio wrapper cannot stop an underlying Python thread.
            return False
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        return True

    def forget(self, job_id: str) -> None:
        """Release a terminal job after its result has been persisted."""
        if not self._job(job_id).done():
            raise ValueError("Cannot forget a running job")
        del self._jobs[job_id]
        self._keys = {key: value for key, value in self._keys.items() if value != job_id}


class AgentTool(LocalFunctionTool):
    """Expose an application-configured Pydantic AI specialist as a model tool."""

    def __init__(
        self,
        agent: Any,
        *,
        deps_factory: Callable[[ToolInput, Services], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        from ..contracts import ToolKind

        async def invoke(value: ToolInput, services: Services) -> ToolResult:
            deps = deps_factory(value, services) if deps_factory else services
            response = await agent.run(value.model_dump_json(), deps=deps)
            output = response.output
            if isinstance(output, ToolResult):
                return output
            if isinstance(output, BaseModel):
                output = output.model_dump(mode="json")
            return ToolResult(payload=output)

        super().__init__(invoke, kind=ToolKind.SUB_AGENT, **kwargs)
