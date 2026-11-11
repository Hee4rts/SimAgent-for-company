"""Numeric metrics and acceptance decisions are calculated by Python, not the LLM."""

from __future__ import annotations

import math
import operator
import statistics
from collections.abc import Mapping

from ..schemas import (
    AcceptanceResult,
    BusinessInput,
    Evaluation,
    MetricValue,
    SimulationPlan,
    ToolResult,
)
from .references import resolve

COMPARISONS = {
    "lt": operator.lt,
    "le": operator.le,
    "eq": operator.eq,
    "ge": operator.ge,
    "gt": operator.gt,
}
REDUCERS = {"sum": sum, "mean": statistics.fmean, "min": min, "max": max}


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Metric input must be numeric, excluding booleans")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Metric input must be finite")
    return number


class MetricEvaluator:
    def evaluate(
        self,
        plan: SimulationPlan,
        request: BusinessInput,
        results: Mapping[str, ToolResult],
    ) -> Evaluation:
        evaluation = Evaluation()
        for definition in plan.metrics:
            try:
                raw = resolve(definition.source, request, results)
                if definition.reducer == "value":
                    value = _number(raw)
                else:
                    if not isinstance(raw, list):
                        raise ValueError("Aggregate metrics require a JSON array")
                    if definition.reducer == "count":
                        value = float(len(raw))
                    else:
                        if not raw:
                            raise ValueError("Numeric aggregates require a nonempty array")
                        value = _number(REDUCERS[definition.reducer]([_number(x) for x in raw]))
                evaluation.metrics[definition.name] = MetricValue(
                    name=definition.name,
                    value=value,
                    unit=definition.unit,
                    source=definition.source,
                )
            except (ValueError, KeyError, IndexError, OverflowError) as error:
                evaluation.errors.append(f"Metric {definition.name}: {error}")
        for criterion in plan.acceptance:
            metric = evaluation.metrics.get(criterion.metric)
            if metric is None:
                evaluation.errors.append(f"Cannot evaluate acceptance for {criterion.metric}")
                continue
            evaluation.acceptance.append(
                AcceptanceResult(
                    criterion=criterion,
                    actual=metric.value,
                    passed=COMPARISONS[criterion.operator](metric.value, criterion.threshold),
                )
            )
        return evaluation
