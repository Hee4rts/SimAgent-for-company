"""Bounded plan → execute → evaluate → revise → interpret control loop."""

from __future__ import annotations

from .agents import PlanSolveAgent, ResultInterpreter
from .contracts import (
    BusinessInput,
    ExecutionFeedback,
    RunEvent,
    RunRecord,
    RunState,
    SimulationPlan,
    TaskState,
    WorkflowResult,
)
from .evaluation import MetricEvaluator
from .executor import SimulationExecutor


class SimulationFramework:
    def __init__(
        self,
        planner: PlanSolveAgent,
        executor: SimulationExecutor,
        interpreter: ResultInterpreter | None = None,
        *,
        evaluator: MetricEvaluator | None = None,
        max_revisions: int = 2,
    ) -> None:
        if not 0 <= max_revisions <= 10:
            raise ValueError("max_revisions must be between 0 and 10")
        if planner.registry is not executor.registry or planner.validator is not executor.validator:
            raise ValueError("Planner and executor must share the same registry and validator")
        self.planner, self.executor, self.interpreter = planner, executor, interpreter
        self.evaluator = evaluator or MetricEvaluator()
        self.max_revisions = max_revisions

    async def run(
        self,
        request: BusinessInput,
        *,
        plan: SimulationPlan | None = None,
    ) -> WorkflowResult:
        plan = plan or await self.planner.plan(request)
        run = self.executor.create_run(request, plan)
        with self.executor.store.exclusive(f"workflow:{run.id}"):
            return await self._complete([await self.executor.execute(run.id)])

    async def resume(self, run_id: str) -> WorkflowResult:
        current = self.executor.get_run(run_id)
        seen = {current.id}
        while current.parent_run_id:
            parent_id = current.parent_run_id
            if parent_id in seen:
                raise ValueError("Run lineage contains a cycle")
            seen.add(parent_id)
            current = self.executor.get_run(parent_id)
        with self.executor.store.exclusive(f"workflow:{current.id}"):
            runs = [self.executor.get_run(current.id)]
            seen = {current.id}
            while runs[-1].next_run_id:
                next_id = runs[-1].next_run_id
                if next_id in seen:
                    raise ValueError("Run lineage contains a cycle")
                seen.add(next_id)
                child = self.executor.get_run(next_id)
                if child.parent_run_id != runs[-1].id:
                    raise ValueError("Run lineage contains an invalid parent link")
                runs.append(child)
            runs[-1] = await self.executor.resume(runs[-1].id)
            return await self._complete(runs)

    async def _complete(self, runs: list[RunRecord]) -> WorkflowResult:
        while True:
            run = runs[-1]
            results = self.executor.results(run)
            evaluation = self.evaluator.evaluate(run.plan, run.request, results)
            run.evaluation = evaluation
            self.executor.store.save(
                run,
                RunEvent(
                    kind="run.evaluated",
                    details={
                        "evaluation": evaluation.model_dump(mode="json"),
                        "passed": evaluation.passed,
                    },
                ),
            )
            if run.state in {RunState.INTERRUPTED, RunState.CANCELLED}:
                return WorkflowResult(runs=runs, evaluation=evaluation)
            if (run.state == RunState.SUCCEEDED and evaluation.passed) or len(
                runs
            ) - 1 >= self.max_revisions:
                break
            issues = list(evaluation.errors)
            issues.extend(
                item.criterion.description for item in evaluation.acceptance if not item.passed
            )
            if run.state != RunState.SUCCEEDED:
                issues.append(f"Run ended with state {run.state.value}")
            feedback = ExecutionFeedback(
                run_id=run.id,
                issues=issues,
                evaluation=evaluation,
                failed_tasks={
                    key: value.error or value.state.value
                    for key, value in run.tasks.items()
                    if value.state != TaskState.SUCCEEDED
                },
            )
            self.executor.store.save(
                run,
                RunEvent(
                    kind="run.replanning",
                    details={"feedback": feedback.model_dump(mode="json")},
                ),
            )
            plan = await self.planner.plan(run.request, previous_plan=run.plan, feedback=feedback)
            revised = self.executor.create_run(run.request, plan, parent_run_id=run.id)
            run.next_run_id = revised.id
            runs.append(await self.executor.execute(revised.id))
        if self.interpreter is not None and run.interpretation is None:
            try:
                run.interpretation = await self.interpreter.interpret(run, results, evaluation)
            except Exception as error:
                self.executor.store.save(
                    run,
                    RunEvent(
                        kind="run.interpretation_failed",
                        details={
                            "error": f"{type(error).__name__}: {error}",
                        },
                    ),
                )
                raise
            self.executor.store.save(run, RunEvent(kind="run.interpreted"))
        return WorkflowResult(runs=runs, evaluation=evaluation, interpretation=run.interpretation)
