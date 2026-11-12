"""Plans, task dependencies, metrics and native-tool catalog descriptions."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .base import Contract, Identifier, JsonPointer, Positive

class ValueReference(Contract):
    """RFC 6901 pointer into the business input or an upstream result's payload."""

    source: Literal["input", "task"]
    task_id: Identifier | None = None
    pointer: JsonPointer = ""
    unit: str | None = None

    @model_validator(mode="after")
    def check_source(self) -> ValueReference:
        if (self.source == "task") != (self.task_id is not None):
            raise ValueError("Only task references must include task_id")
        return self


class RetryPolicy(Contract):
    max_attempts: int = Field(default=1, ge=1, le=20)
    initial_delay_seconds: float = Field(default=1, ge=0)
    max_delay_seconds: float = Field(default=30, ge=0)
    backoff: float = Field(default=2, ge=1)

    def delay(self, attempt: int) -> float:
        return min(
            self.initial_delay_seconds * self.backoff ** (attempt - 1), self.max_delay_seconds
        )


class SimulationTask(Contract):
    id: Identifier
    description: str
    tool: Identifier
    tool_version: str = Field(min_length=1)
    depends_on: list[Identifier] = Field(default_factory=list)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    bindings: dict[str, ValueReference] = Field(default_factory=dict)
    parameter_units: dict[str, str] = Field(default_factory=dict)
    resources: dict[str, Positive] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=3600, gt=0)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class MetricDefinition(Contract):
    name: Identifier
    description: str
    source: ValueReference
    reducer: Literal["value", "sum", "mean", "min", "max", "count"] = "value"
    unit: str | None = None


class AcceptanceCriterion(Contract):
    metric: Identifier
    operator: Literal["lt", "le", "eq", "ge", "gt"]
    threshold: float
    description: str


class RunConfig(Contract):
    max_concurrency: int = Field(default=4, ge=1, le=256)
    poll_interval_seconds: float = Field(default=1, gt=0)
    seed: int = Field(default=0, ge=0)
    fail_fast: bool = True


class PlanningReply(Contract):
    """A planner's response when there is no executable task yet."""

    message: str = Field(min_length=1)
    questions: list[str] = Field(default_factory=list)
    reason: Literal["missing_input", "unsupported", "discussion"]


class SimulationPlan(Contract):
    title: str = Field(min_length=1)
    goals: list[str] = Field(min_length=1)
    scope: str
    assumptions: list[str] = Field(default_factory=list)
    tasks: list[SimulationTask] = Field(min_length=1)
    metrics: list[MetricDefinition] = Field(default_factory=list)
    acceptance: list[AcceptanceCriterion] = Field(default_factory=list)
    config: RunConfig = Field(default_factory=RunConfig)
    versions: dict[str, str] = Field(default_factory=dict)


class ToolDescriptor(Contract):
    """Serializable plan catalog derived from a native Tool; not a registration type."""

    name: Identifier
    description: str
    tags: list[str] = Field(default_factory=list)
    version: str = Field(min_length=1)
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    parameter_units: dict[str, str] = Field(default_factory=dict)
    output_units: dict[str, str] = Field(default_factory=dict)
    idempotent: bool = False
