"""Job states, persisted run records, events and complete workflow results."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, JsonValue

from .base import Contract, new_id, now
from .business import BusinessInput
from .plan import PlanningReply, SimulationPlan
from .results import Evaluation, Interpretation

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


class WorkflowResult(Contract):
    runs: list[RunRecord]
    evaluation: Evaluation
    interpretation: Interpretation | None = None
    reply: PlanningReply | None = None

    @property
    def succeeded(self) -> bool:
        return (
            bool(self.runs) and self.runs[-1].state == RunState.SUCCEEDED and self.evaluation.passed
            and self.reply is None
        )
