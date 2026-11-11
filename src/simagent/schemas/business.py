"""Business inputs, data references and per-task execution inputs."""

from __future__ import annotations

from pydantic import Field, JsonValue

from .base import Contract, Identifier, Positive, new_id

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


class ToolInput(Contract):
    """Execution envelope; ordinary functions receive only their declared arguments."""

    run_id: str = Field(default_factory=new_id)
    task_id: str = Field(default_factory=new_id)
    scenario: dict[str, JsonValue] = Field(default_factory=dict)
    initial_state: dict[str, JsonValue] = Field(default_factory=dict)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    parameter_units: dict[str, str] = Field(default_factory=dict)
    data: dict[str, DataReference] = Field(default_factory=dict)
    seed: int = 0
    versions: dict[str, str] = Field(default_factory=dict)
