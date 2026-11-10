"""Serializable contracts shared by the agents, executor and model tools."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Identifier = Annotated[str, Field(min_length=1, pattern=r"^[a-zA-Z0-9_.-]+$")]
Positive = Annotated[float, Field(gt=0)]
JsonPointer = Annotated[str, Field(pattern=r"^(?:/(?:[^~]|~[01])*)?$")]


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid4().hex


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DataReference(Contract):
    uri: str = Field(min_length=1)
    version: str = Field(min_length=1)
    media_type: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class BusinessConstraint(Contract):
    name: Identifier
    description: str
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class BusinessInput(Contract):
    question: str = Field(min_length=1)
    goals: list[str] = Field(min_length=1)
    scenario: dict[str, JsonValue] = Field(default_factory=dict)
    initial_state: dict[str, JsonValue] = Field(default_factory=dict)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    parameter_units: dict[str, str] = Field(default_factory=dict)
    data: dict[str, DataReference] = Field(default_factory=dict)
    resource_limits: dict[str, Positive] = Field(default_factory=dict)
    constraints: list[BusinessConstraint] = Field(default_factory=list)
    knowledge_refs: list[str] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)


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


class ToolKind(StrEnum):
    MACHINE_LEARNING = "machine_learning"
    MECHANISTIC = "mechanistic"
    HYBRID = "hybrid"
    SUB_AGENT = "sub_agent"


class ToolDescriptor(Contract):
    name: Identifier
    description: str
    kind: ToolKind
    version: str = Field(min_length=1)
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    parameter_units: dict[str, str] = Field(default_factory=dict)
    output_units: dict[str, str] = Field(default_factory=dict)
    resumable: bool = False
    idempotent: bool = False


class ToolInput(Contract):
    run_id: str
    task_id: str
    scenario: dict[str, JsonValue]
    initial_state: dict[str, JsonValue]
    parameters: dict[str, JsonValue]
    parameter_units: dict[str, str]
    data: dict[str, DataReference]
    seed: int
    versions: dict[str, str]


class Diagnostic(Contract):
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "info"
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResult(Contract):
    payload: dict[str, JsonValue]
    output_units: dict[str, str] = Field(default_factory=dict)
    artifacts: list[DataReference] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobUpdate(Contract):
    job_id: str = Field(min_length=1)
    state: JobState
    message: str | None = None
    retryable: bool = False


class TaskState(StrEnum):
    PENDING = "pending"
    SUBMITTING = "submitting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


class RunState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class TaskRecord(Contract):
    task_id: str
    state: TaskState = TaskState.PENDING
    attempt: int = 0
    idempotency_key: str | None = None
    job_id: str | None = None
    result_ref: str | None = None
    started_at: datetime | None = None
    deadline_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class RunRecord(Contract):
    id: str = Field(default_factory=new_id)
    parent_run_id: str | None = None
    next_run_id: str | None = None
    request: BusinessInput
    plan: SimulationPlan
    state: RunState = RunState.CREATED
    tasks: dict[str, TaskRecord]
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    error: str | None = None
    evaluation: Evaluation | None = None
    interpretation: Interpretation | None = None


class RunEvent(Contract):
    timestamp: datetime = Field(default_factory=now)
    kind: str
    task_id: str | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class MetricValue(Contract):
    name: str
    value: float
    unit: str | None = None
    source: ValueReference


class AcceptanceResult(Contract):
    criterion: AcceptanceCriterion
    actual: float
    passed: bool


class Evaluation(Contract):
    metrics: dict[str, MetricValue] = Field(default_factory=dict)
    acceptance: list[AcceptanceResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors and all(item.passed for item in self.acceptance)


class Evidence(Contract):
    task_id: str
    pointer: JsonPointer = ""
    metric: str | None = None


class Finding(Contract):
    statement: str
    evidence: list[Evidence] = Field(min_length=1)


class Interpretation(Contract):
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ExecutionFeedback(Contract):
    run_id: str
    issues: list[str]
    failed_tasks: dict[str, str] = Field(default_factory=dict)
    evaluation: Evaluation | None = None


class WorkflowResult(Contract):
    runs: list[RunRecord]
    evaluation: Evaluation
    interpretation: Interpretation | None = None

    @property
    def succeeded(self) -> bool:
        return (
            bool(self.runs) and self.runs[-1].state == RunState.SUCCEEDED and self.evaluation.passed
        )


RunRecord.model_rebuild()
