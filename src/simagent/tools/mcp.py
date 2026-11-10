"""Optional MCP lifecycle adapter. The application owns the connected ClientSession."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..contracts import JobState, JobUpdate, ToolInput, ToolResult
from .base import ModelTool

if TYPE_CHECKING:
    from mcp import ClientSession


@dataclass(frozen=True)
class MCPJobMethods:
    submit: str
    status: str
    result: str
    cancel: str


class MCPJobTool(ModelTool):
    def __init__(
        self,
        *,
        session: ClientSession,
        methods: MCPJobMethods,
        **kwargs: Any,
    ) -> None:
        super().__init__(resumable=True, idempotent=True, **kwargs)
        self.session = session
        self.methods = methods

    async def _call(self, method: str, arguments: dict[str, Any]) -> Any:
        result = await self.session.call_tool(method, arguments)
        if result.isError:
            # The remote side may already have submitted a job: preserve uncertainty.
            raise RuntimeError(f"MCP method failed: {method}")
        if result.structuredContent is not None:
            return result.structuredContent
        text = [item.text for item in result.content if item.type == "text"]
        if len(text) != 1:
            raise ValueError(f"MCP method {method} must return one JSON object")
        return json.loads(text[0])

    async def submit(self, value: ToolInput, *, idempotency_key: str) -> str:
        output = await self._call(
            self.methods.submit,
            {
                "input": self.validate_input(value).model_dump(mode="json"),
                "idempotency_key": idempotency_key,
            },
        )
        return JobUpdate.model_validate(output).job_id

    async def status(self, job_id: str) -> JobUpdate:
        return JobUpdate.model_validate(await self._call(self.methods.status, {"job_id": job_id}))

    async def result(self, job_id: str) -> ToolResult:
        output = await self._call(self.methods.result, {"job_id": job_id})
        return self.validate_result(ToolResult.model_validate(output))

    async def cancel(self, job_id: str) -> bool:
        output = await self._call(self.methods.cancel, {"job_id": job_id})
        status = JobUpdate.model_validate(output)
        return status.job_id == job_id and status.state in {
            JobState.CANCELLED,
            JobState.FAILED,
            JobState.SUCCEEDED,
        }
