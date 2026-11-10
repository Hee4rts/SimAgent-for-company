"""Deterministic plan validation, separate from LLM output type validation."""

from __future__ import annotations

import json
from collections.abc import Callable
from graphlib import CycleError, TopologicalSorter

from pydantic import TypeAdapter, ValidationError

from .contracts import (
    BusinessConstraint,
    BusinessInput,
    SimulationPlan,
    SimulationTask,
    ToolInput,
    ValueReference,
)
from .errors import PlanValidationError
from .references import resolve
from .tools.registry import ToolRegistry

ConstraintCheck = Callable[[SimulationPlan, BusinessInput, BusinessConstraint], list[str]]
InputCheck = Callable[[SimulationTask, ToolInput, BusinessInput], list[str]]


class PlanValidator:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self._constraints: dict[str, ConstraintCheck] = {}
        self._input_checks: list[InputCheck] = []

    def register_constraint(self, name: str, check: ConstraintCheck) -> None:
        if name in self._constraints:
            raise ValueError(f"Constraint already registered: {name}")
        self._constraints[name] = check

    def register_input_check(self, check: InputCheck) -> None:
        """Checks run after upstream outputs have been bound, before submission."""
        self._input_checks.append(check)

    def validate_input(
        self,
        task: SimulationTask,
        value: ToolInput,
        request: BusinessInput,
    ) -> ToolInput:
        tool = self.registry.get(task.tool)
        validated = tool.validate_input(value)
        issues = [
            issue for check in self._input_checks for issue in check(task, validated, request)
        ]
        if issues:
            raise PlanValidationError(issues)
        return validated

    def validate(self, plan: SimulationPlan, request: BusinessInput) -> None:
        issues: list[str] = []
        tasks = {task.id: task for task in plan.tasks}
        if len(tasks) != len(plan.tasks):
            issues.append("Task IDs must be unique")
        try:
            tuple(TopologicalSorter({t.id: t.depends_on for t in plan.tasks}).static_order())
        except CycleError:
            issues.append("Task dependencies contain a cycle")
        for key, version in plan.versions.items():
            if key in request.versions and version != request.versions[key]:
                issues.append(f"Plan cannot override input version {key!r}")

        for task in plan.tasks:
            prefix = f"Task {task.id}: "
            if len(set(task.depends_on)) != len(task.depends_on):
                issues.append(prefix + "duplicate dependencies")
            if task.id in task.depends_on:
                issues.append(prefix + "cannot depend on itself")
            for dependency in task.depends_on:
                if dependency not in tasks:
                    issues.append(prefix + f"unknown dependency {dependency}")
            for resource, amount in task.resources.items():
                limit = request.resource_limits.get(resource)
                if limit is None or amount > limit:
                    issues.append(
                        prefix + f"resource {resource!r} has insufficient/unknown capacity"
                    )
            try:
                tool = self.registry.get(task.tool)
            except KeyError as error:
                issues.append(prefix + str(error))
                continue
            if task.tool_version != tool.descriptor.version:
                issues.append(prefix + "tool version does not match registry")
            if task.retry.max_attempts > 1 and not tool.descriptor.idempotent:
                issues.append(prefix + "retries require an idempotent tool")
            if set(task.parameters) & set(task.bindings):
                issues.append(prefix + "parameters and bindings must have distinct names")
            parameters = dict(task.parameters)
            unresolved: set[str] = set()
            for name, reference in task.bindings.items():
                issues.extend(
                    prefix + issue for issue in self._reference(reference, request, tasks)
                )
                if reference.source == "task":
                    if reference.task_id not in task.depends_on:
                        issues.append(
                            prefix + f"binding {name!r} must declare its producer in depends_on"
                        )
                    unresolved.add(name)
                else:
                    try:
                        parameters[name] = resolve(reference, request, {})
                    except (ValueError, KeyError, IndexError) as error:
                        issues.append(prefix + f"binding {name!r}: {error}")
                target_unit = task.parameter_units.get(name)
                if target_unit != reference.unit:
                    issues.append(prefix + f"binding {name!r} must preserve its source unit")
            for name, unit in tool.descriptor.parameter_units.items():
                if task.parameter_units.get(name) != unit:
                    issues.append(prefix + f"parameter {name!r} requires unit {unit!r}")
            fields = tool.parameter_model.model_fields
            available = set(parameters) | unresolved
            for name, field in fields.items():
                if field.is_required() and name not in available:
                    issues.append(prefix + f"missing parameter {name!r}")
            if tool.parameter_model.model_config.get("extra") != "allow":
                for name in available - fields.keys():
                    issues.append(prefix + f"unknown parameter {name!r}")
            try:
                if not unresolved:
                    tool.parameter_model.model_validate_json(
                        json.dumps(parameters), strict=True, by_name=True
                    )
                else:
                    for name, value in parameters.items():
                        if name in fields:
                            TypeAdapter(fields[name].rebuild_annotation()).validate_json(
                                json.dumps(value),
                                strict=True,
                            )
            except (ValueError, ValidationError) as error:
                issues.append(prefix + f"invalid parameters: {error}")

        metrics = {metric.name: metric for metric in plan.metrics}
        if len(metrics) != len(plan.metrics):
            issues.append("Metric names must be unique")
        for metric in plan.metrics:
            issues.extend(
                f"Metric {metric.name}: {issue}"
                for issue in self._reference(metric.source, request, tasks)
            )
            if metric.source.source != "task":
                issues.append(f"Metric {metric.name}: metrics must reference tool results")
            expected = None if metric.reducer == "count" else metric.source.unit
            if metric.unit != expected:
                issues.append(
                    f"Metric {metric.name}: unit must match source; count is dimensionless"
                )
        for criterion in plan.acceptance:
            if criterion.metric not in metrics:
                issues.append(f"Acceptance references unknown metric {criterion.metric!r}")
        for constraint in request.constraints:
            check = self._constraints.get(constraint.name)
            if check is None:
                issues.append(f"No validator registered for hard constraint {constraint.name!r}")
            else:
                issues.extend(check(plan, request, constraint))
        if issues:
            raise PlanValidationError(issues)

    def _reference(
        self,
        reference: ValueReference,
        request: BusinessInput,
        tasks: dict[str, SimulationTask],
    ) -> list[str]:
        if reference.source == "input":
            actual_unit = None
            if reference.pointer.startswith("/parameters/"):
                key = reference.pointer[len("/parameters/") :].replace("~1", "/").replace("~0", "~")
                actual_unit = request.parameter_units.get(key)
        else:
            if reference.task_id not in tasks:
                return [f"unknown result source {reference.task_id!r}"]
            task = tasks[reference.task_id]
            try:
                descriptor = self.registry.get(task.tool).descriptor
            except KeyError:
                return [f"unknown source tool {task.tool!r}"]
            actual_unit = descriptor.output_units.get(reference.pointer)
        if actual_unit != reference.unit:
            return [
                f"source unit at {reference.pointer!r} is {actual_unit!r}, "
                f"reference declares {reference.unit!r}"
            ]
        return []
