"""Shared agent limits and bounded tool responses."""

from __future__ import annotations

import json

from pydantic import Field, JsonValue
from pydantic_ai import ModelRetry
from pydantic_ai.usage import UsageLimits

from ..schemas.base import Contract

class AgentLimits(Contract):
    output_retries: int = Field(default=2, ge=0, le=10)
    request_limit: int = Field(default=20, ge=1)
    tool_calls_limit: int = Field(default=40, ge=1)
    timeout_seconds: float = Field(default=180, gt=0)
    tool_timeout_seconds: float = Field(default=30, gt=0)
    max_tool_response_characters: int = Field(default=50_000, ge=100)

    def usage(self) -> UsageLimits:
        return UsageLimits(request_limit=self.request_limit, tool_calls_limit=self.tool_calls_limit)


def _bounded(value: JsonValue, limit: int) -> JsonValue:
    if len(json.dumps(value, ensure_ascii=False)) > limit:
        raise ModelRetry("Result is too large. Select a narrower JSON pointer or query.")
    return value
