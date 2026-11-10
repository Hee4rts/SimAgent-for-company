"""One lifecycle for local models, remote simulation jobs and specialist agents."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

from pydantic import BaseModel

from ..contracts import JobUpdate, ToolDescriptor, ToolInput, ToolKind, ToolResult


class ModelTool(ABC):
    def __init__(
        self,
        *,
        name: str,
        description: str,
        kind: ToolKind,
        version: str,
        parameter_model: type[BaseModel],
        result_model: type[BaseModel],
        parameter_units: dict[str, str] | None = None,
        output_units: dict[str, str] | None = None,
        resumable: bool = False,
        idempotent: bool = False,
    ) -> None:
        self.parameter_model = parameter_model
        self.result_model = result_model
        self.descriptor = ToolDescriptor(
            name=name,
            description=description,
            kind=kind,
            version=version,
            input_schema=parameter_model.model_json_schema(by_alias=False),
            output_schema=result_model.model_json_schema(by_alias=False),
            parameter_units=parameter_units or {},
            output_units=output_units or {},
            resumable=resumable,
            idempotent=idempotent,
        )

    def validate_input(self, value: ToolInput) -> ToolInput:
        parameters = self.parameter_model.model_validate_json(
            json.dumps(value.parameters), strict=True, by_name=True
        )
        for name, expected in self.descriptor.parameter_units.items():
            if value.parameter_units.get(name) != expected:
                raise ValueError(f"Parameter {name!r} requires unit {expected!r}")
        return value.model_copy(update={"parameters": parameters.model_dump(mode="json")})

    def validate_result(self, value: ToolResult) -> ToolResult:
        payload = self.result_model.model_validate_json(
            json.dumps(value.payload), strict=True, by_name=True
        )
        units = dict(self.descriptor.output_units)
        for pointer, unit in value.output_units.items():
            if pointer in units and units[pointer] != unit:
                raise ValueError(f"Output unit mismatch at {pointer!r}")
            units[pointer] = unit
        versions = dict(value.versions)
        key = f"tool:{self.descriptor.name}"
        if key in versions and versions[key] != self.descriptor.version:
            raise ValueError(f"Tool version mismatch: {key}")
        versions[key] = self.descriptor.version
        return value.model_copy(
            update={
                "payload": payload.model_dump(mode="json"),
                "output_units": units,
                "versions": versions,
            }
        )

    @abstractmethod
    async def submit(self, value: ToolInput, *, idempotency_key: str) -> str:
        """Return a job ID; durable tools must deduplicate an identical submission key."""

    @abstractmethod
    async def status(self, job_id: str) -> JobUpdate: ...

    @abstractmethod
    async def result(self, job_id: str) -> ToolResult: ...

    @abstractmethod
    async def cancel(self, job_id: str) -> bool:
        """True only when the job is known to have stopped; False means uncertain."""
