"""Tool outputs, evaluation, evidence, interpretation and execution feedback."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from .base import Contract, JsonPointer
from .business import DataReference
from .plan import AcceptanceCriterion, ValueReference

class Diagnostic(Contract):
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "info"
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResult(Contract):
    payload: JsonValue
    output_units: dict[str, str] = Field(default_factory=dict)
    artifacts: list[DataReference] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)


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
