"""Internal plan execution helpers for official Pydantic AI tools.

This module has no registration API. FunctionToolset owns all tool definitions;
the executor only keeps handles to running Python calls here.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace
from typing import Any

from pydantic import JsonValue, ValidationError
from pydantic_core import to_jsonable_python
from pydantic_ai import RunContext, Tool
from pydantic_ai.messages import ToolReturn
from pydantic_ai.models import Model
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RunUsage

from ..schemas import JobState, JobUpdate, ToolDescriptor, ToolInput, ToolResult, new_id
from ..infrastructure.dependencies import Services
from ..errors import JobNotFoundError, ToolExecutionError


def describe_tool(tool: Tool[Services]) -> ToolDescriptor:
    """Read schemas and optional plan metadata from a native Tool."""
    metadata = (tool.metadata or {}).get("simagent", {})
    if not isinstance(metadata, dict):
        raise ValueError(f"Tool {tool.name!r}: metadata['simagent'] must be a dictionary")
    output_schema = dict(tool.function_schema.return_schema)
    if output_schema.get("title") == "ToolResult":
        output_schema = {
            **output_schema.get("properties", {}).get("payload", {}),
            "$defs": output_schema.get("$defs", {}),
        }
    return ToolDescriptor(
        name=tool.name, description=tool.description or "",
        input_schema=tool.function_schema.json_schema, output_schema=output_schema,
        version=metadata.get("version", "1"), tags=metadata.get("tags", []),
        idempotent=metadata.get("idempotent", False),
        parameter_units=metadata.get("parameter_units", {}),
        output_units=metadata.get("output_units", {}),
    )


def parameter_issues(
    tool: Tool[Services], parameters: dict[str, JsonValue], unresolved: set[str],
) -> list[str]:
    """Defer upstream bindings; validate known values with the native validator."""
    schema = tool.function_schema.json_schema
    issues: list[str] = []
    if unresolved and schema.get("additionalProperties") is False:
        properties = schema.get("properties", {})
        issues.extend(f"unknown parameter {name!r}" for name in unresolved - properties.keys())
    try:
        tool.function_schema.validator.validate_json(json.dumps(parameters, allow_nan=False))
    except ValidationError as error:
        for item in error.errors(include_url=False, include_input=False):
            location = item["loc"]
            if unresolved and (not location or location[0] in unresolved):
                continue
            path = ".".join(str(part) for part in location) or "arguments"
            issues.append(f"{path}: {item['msg']}")
    except ValueError as error:
        if not unresolved:
            issues.append(str(error))
    return issues


def validate_tool_input(tool: Tool[Services], value: ToolInput) -> ToolInput:
    tool.function_schema.validator.validate_json(json.dumps(value.parameters, allow_nan=False))
    for name, expected in describe_tool(tool).parameter_units.items():
        if value.parameter_units.get(name) != expected:
            raise ValueError(f"Parameter {name!r} requires unit {expected!r}")
    # Preserve the published JSON shape, including native flattened model arguments.
    return value.model_copy(deep=True)


def _result(output: Any, descriptor: ToolDescriptor) -> ToolResult:
    if isinstance(output, ToolReturn):
        output = output.return_value
    value = output if isinstance(output, ToolResult) else ToolResult(payload=to_jsonable_python(output))
    json.dumps(value.payload, allow_nan=False)
    units = dict(descriptor.output_units)
    for pointer, unit in value.output_units.items():
        if pointer in units and units[pointer] != unit:
            raise ValueError(f"Output unit mismatch at {pointer!r}")
        units[pointer] = unit
    versions = dict(value.versions)
    key = f"tool:{descriptor.name}"
    if key in versions and versions[key] != descriptor.version:
        raise ValueError(f"Tool version mismatch: {key}")
    versions[key] = descriptor.version
    return value.model_copy(deep=True, update={"output_units": units, "versions": versions})


def _no_model_request(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError(
        "Standalone plan execution has no language model. Pass execution_model "
        "to SimulationExecutor or configure the delegated Agent's model."
    )


class _FunctionJob:
    """In-process task handles owned by SimulationExecutor, never registered as tools."""

    def __init__(
        self, toolset: FunctionToolset[Services], tool: Tool[Services],
        services: Services, model: Model | None,
    ) -> None:
        self.toolset, self.tool, self.services = toolset, tool, services
        self.descriptor = describe_tool(tool)
        self.model = model if model is not None else FunctionModel(
            _no_model_request, model_name="simagent-standalone-tools",
        )
        self._jobs: dict[str, asyncio.Task[ToolResult]] = {}
        self._keys: dict[str, str] = {}

    async def submit(self, value: ToolInput, *, idempotency_key: str) -> str:
        if idempotency_key in self._keys:
            return self._keys[idempotency_key]
        value = validate_tool_input(self.tool, value)
        job_id = new_id()
        task = asyncio.create_task(self._invoke(value, idempotency_key), name=f"tool:{job_id}")
        task.add_done_callback(lambda job: None if job.cancelled() else job.exception())
        self._jobs[job_id] = task
        self._keys[idempotency_key] = job_id
        return job_id

    async def _invoke(self, value: ToolInput, key: str) -> ToolResult:
        ctx = RunContext(
            deps=replace(self.services, input=value, idempotency_key=key),
            model=self.model, usage=RunUsage(), run_id=value.run_id,
            tool_name=self.tool.name, tool_call_id=key,
            max_retries=(
                self.tool.max_retries
                if self.tool.max_retries is not None else (self.toolset.max_retries or 0)
            ),
        )
        definition = await self.tool.prepare_tool_def(ctx)
        if definition is None:
            raise ToolExecutionError("Tool is unavailable in this execution context")
        if definition.kind != "function":
            raise ToolExecutionError("Deferred/approval tools require a native Agent flow")
        if definition.sequential and not self.tool.sequential:
            raise ToolExecutionError("Set sequential=True at registration for planned execution")
        prepared = self.toolset.tool_for_tool_def(definition, ctx=ctx, original_name=self.tool.name)
        ctx.tool_name = definition.name
        ctx.max_retries = prepared.max_retries
        args = prepared.args_validator.validate_json(json.dumps(value.parameters, allow_nan=False))
        if prepared.args_validator_func is not None:
            validated = prepared.args_validator_func(ctx, **args)
            if inspect.isawaitable(validated):
                await validated
        if self.tool.function_schema.is_async:
            output = await self.toolset.call_tool(definition.name, args, ctx, prepared)
        else:
            # Keep a handle to the worker thread: native timeout abandonment would
            # otherwise report a stopped job while its Python code keeps running.
            output = await self.tool.function_schema.call(args, ctx)
        return _result(output, self.descriptor)

    def _job(self, job_id: str) -> asyncio.Task[ToolResult]:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise JobNotFoundError(f"In-process job is unavailable: {job_id}") from None

    async def status(self, job_id: str) -> JobUpdate:
        job = self._job(job_id)
        if job.cancelled():
            return JobUpdate(job_id=job_id, state=JobState.CANCELLED)
        if not job.done():
            return JobUpdate(job_id=job_id, state=JobState.RUNNING)
        error = job.exception()
        return JobUpdate(
            job_id=job_id, state=JobState.FAILED if error else JobState.SUCCEEDED,
            message=str(error) if error else None,
            retryable=isinstance(error, ToolExecutionError) and error.retryable,
        )

    async def result(self, job_id: str) -> ToolResult:
        return (await asyncio.shield(self._job(job_id))).model_copy(deep=True)

    async def cancel(self, job_id: str) -> bool:
        job = self._job(job_id)
        if job.done():
            return True
        if not self.tool.function_schema.is_async:
            return False
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        return job.done()
