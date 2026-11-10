"""HTTP job protocol: POST /jobs, GET /jobs/{id}, GET /result, DELETE /jobs/{id}."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from ..contracts import JobState, JobUpdate, ToolInput, ToolResult
from ..errors import JobNotFoundError, ToolExecutionError
from .base import ModelTool


class HTTPJobTool(ModelTool):
    def __init__(self, *, client: httpx.AsyncClient, jobs_url: str, **kwargs: Any) -> None:
        super().__init__(resumable=True, idempotent=True, **kwargs)
        self.client = client
        self.jobs_url = jobs_url.rstrip("/")

    def _url(self, job_id: str) -> str:
        return f"{self.jobs_url}/{quote(job_id, safe='')}"

    async def submit(self, value: ToolInput, *, idempotency_key: str) -> str:
        value = self.validate_input(value)
        response = await self.client.post(
            self.jobs_url,
            json=value.model_dump(mode="json"),
            headers={"Idempotency-Key": idempotency_key},
        )
        if response.status_code in {400, 401, 403, 404, 405, 413, 415, 422, 429}:
            raise ToolExecutionError(
                f"Job submission rejected: HTTP {response.status_code}",
                retryable=response.status_code == 429,
            )
        response.raise_for_status()
        return JobUpdate.model_validate(response.json()).job_id

    async def status(self, job_id: str) -> JobUpdate:
        response = await self.client.get(self._url(job_id))
        if response.status_code == 404:
            raise JobNotFoundError(f"Remote job is unavailable: {job_id}")
        response.raise_for_status()
        return JobUpdate.model_validate(response.json())

    async def result(self, job_id: str) -> ToolResult:
        response = await self.client.get(f"{self._url(job_id)}/result")
        response.raise_for_status()
        return self.validate_result(ToolResult.model_validate(response.json()))

    async def cancel(self, job_id: str) -> bool:
        response = await self.client.delete(self._url(job_id))
        response.raise_for_status()
        status = JobUpdate.model_validate(response.json())
        return status.job_id == job_id and status.state in {
            JobState.CANCELLED,
            JobState.FAILED,
            JobState.SUCCEEDED,
        }
