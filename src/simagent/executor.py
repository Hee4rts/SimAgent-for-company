"""Dependency scheduling and durable job control; no simulation clock lives here."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import timedelta

from pydantic import JsonValue

from .contracts import (
    BusinessInput,
    JobState,
    RunEvent,
    RunRecord,
    RunState,
    SimulationPlan,
    SimulationTask,
    TaskRecord,
    TaskState,
    ToolInput,
    ToolResult,
    now,
)
from .errors import PlanValidationError, RecoveryRequiredError, RunBusyError, ToolExecutionError
from .references import resolve
from .storage import RunStore
from .tools.base import ModelTool
from .tools.registry import ToolRegistry
from .validation import PlanValidator

logger = logging.getLogger(__name__)
TERMINAL_TASKS = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.SKIPPED}
TERMINAL_RUNS = {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}


class SimulationExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        store: RunStore,
        *,
        validator: PlanValidator | None = None,
        cancellation_timeout_seconds: float = 10,
    ) -> None:
        if cancellation_timeout_seconds <= 0:
            raise ValueError("Cancellation timeout must be positive")
        self.registry = registry
        self.store = store
        self.validator = validator or PlanValidator(registry)
        self.cancellation_timeout_seconds = cancellation_timeout_seconds
        self._active: dict[str, tuple[asyncio.Event, asyncio.Event]] = {}

    def create_run(
        self,
        request: BusinessInput,
        plan: SimulationPlan,
        *,
        parent_run_id: str | None = None,
    ) -> RunRecord:
        # Re-validate copies so caller mutations cannot change an executing plan.
        request = BusinessInput.model_validate(request.model_dump(mode="json"))
        plan = SimulationPlan.model_validate(plan.model_dump(mode="json"))
        self.validator.validate(plan, request)
        run = RunRecord(
            request=request,
            plan=plan,
            parent_run_id=parent_run_id,
            tasks={task.id: TaskRecord(task_id=task.id) for task in plan.tasks},
        )
        self.store.create(run)
        return run

    def get_run(self, run_id: str) -> RunRecord:
        return self.store.load(run_id)

    def results(self, run: RunRecord) -> dict[str, ToolResult]:
        results: dict[str, ToolResult] = {}
        for task_id, record in run.tasks.items():
            if record.state == TaskState.SUCCEEDED:
                if record.result_ref is None:
                    raise RecoveryRequiredError(
                        f"Successful task {task_id} has no result reference"
                    )
                results[task_id] = self.store.get_result(record.result_ref)
        return results

    def _save(
        self,
        run: RunRecord,
        kind: str,
        task_id: str | None = None,
        **details: JsonValue,
    ) -> None:
        event = RunEvent(kind=kind, task_id=task_id, details=details)
        self.store.save(run, event)
        logger.info("%s run=%s task=%s", kind, run.id, task_id)

    async def execute(self, run_id: str) -> RunRecord:
        """Execute or resume a saved run. Concurrent ownership of a run is rejected."""
        if run_id in self._active:
            raise RunBusyError(f"Run {run_id} is already executing")
        with self.store.exclusive(run_id):
            run = self.store.load(run_id)
            if run.state in TERMINAL_RUNS:
                return run
            self.validator.validate(run.plan, run.request)
            cancelled, finished = asyncio.Event(), asyncio.Event()
            self._active[run_id] = cancelled, finished
            run.state, run.error = RunState.RUNNING, None
            try:
                self._save(run, "run.started")
                await self._schedule(run, cancelled)
                self._save(run, f"run.{run.state.value}")
                return run
            except BaseException as error:
                run.state = RunState.INTERRUPTED
                run.error = f"Execution interrupted: {type(error).__name__}: {error}"
                self._save(run, "run.interrupted", error=run.error)
                raise
            finally:
                self._active.pop(run_id, None)
                finished.set()

    async def resume(self, run_id: str) -> RunRecord:
        return await self.execute(run_id)

    async def cancel(self, run_id: str) -> RunRecord:
        """Request cancellation in this executor, or cancel a detached saved run."""
        if run_id in self._active:
            cancel_event, finished = self._active[run_id]
            cancel_event.set()
            await finished.wait()
            return self.store.load(run_id)
        with self.store.exclusive(run_id):
            run = self.store.load(run_id)
            if run.state in TERMINAL_RUNS:
                return run
            await self._cancel_records(run)
            run.state = (
                RunState.INTERRUPTED if self._has_uncertain_jobs(run) else RunState.CANCELLED
            )
            self._save(run, f"run.{run.state.value}")
            return run

    @staticmethod
    def _has_uncertain_jobs(run: RunRecord) -> bool:
        return any(record.state == TaskState.INTERRUPTED for record in run.tasks.values())

    async def _schedule(self, run: RunRecord, cancel_event: asyncio.Event) -> None:
        results = self.results(run)
        active: dict[asyncio.Task[None], SimulationTask] = {}
        cancel_waiter = asyncio.create_task(cancel_event.wait())
        try:
            # A process may have stopped after submit but before recording job_id.
            for record in run.tasks.values():
                if record.state == TaskState.INTERRUPTED:
                    record.state = TaskState.RUNNING if record.job_id else TaskState.SUBMITTING
            while True:
                if cancel_event.is_set():
                    await self._drain(active)
                    await self._cancel_records(run)
                    run.state = (
                        RunState.INTERRUPTED
                        if self._has_uncertain_jobs(run)
                        else RunState.CANCELLED
                    )
                    return
                if self._has_uncertain_jobs(run):
                    await self._drain(active)
                    run.state = RunState.INTERRUPTED
                    run.error = "Uncertain jobs require resume or explicit cancellation"
                    return
                failed = any(r.state == TaskState.FAILED for r in run.tasks.values())
                if failed and run.plan.config.fail_fast:
                    await self._drain(active)
                    await self._cancel_records(run)
                    run.state = (
                        RunState.INTERRUPTED if self._has_uncertain_jobs(run) else RunState.FAILED
                    )
                    return

                active_ids = {task.id for task in active.values()}
                skipped = False
                used: dict[str, float] = {}
                for task in active.values():
                    for resource, amount in task.resources.items():
                        used[resource] = used.get(resource, 0) + amount
                # Reattach already submitted jobs before admitting new work.
                ordered = sorted(
                    run.plan.tasks, key=lambda task: run.tasks[task.id].state == TaskState.PENDING
                )
                for task in ordered:
                    record = run.tasks[task.id]
                    if task.id in active_ids or record.state in TERMINAL_TASKS:
                        continue
                    dependencies = [run.tasks[name].state for name in task.depends_on]
                    if any(
                        state in {TaskState.FAILED, TaskState.CANCELLED, TaskState.SKIPPED}
                        for state in dependencies
                    ):
                        record.state, record.error = TaskState.SKIPPED, "Dependency did not succeed"
                        record.finished_at = now()
                        skipped = True
                        self._save(run, "task.skipped", task.id, error=record.error)
                        continue
                    if not all(state == TaskState.SUCCEEDED for state in dependencies):
                        continue
                    if len(active) >= run.plan.config.max_concurrency:
                        break
                    if any(
                        used.get(name, 0) + amount > run.request.resource_limits[name]
                        for name, amount in task.resources.items()
                    ):
                        continue
                    future = asyncio.create_task(self._execute_task(run, task, results))
                    active[future] = task
                    for name, amount in task.resources.items():
                        used[name] = used.get(name, 0) + amount
                if not active:
                    if all(r.state == TaskState.SUCCEEDED for r in run.tasks.values()):
                        run.state = RunState.SUCCEEDED
                    elif all(r.state in TERMINAL_TASKS for r in run.tasks.values()):
                        run.state = RunState.FAILED
                    elif skipped:
                        # Plans need not list tasks topologically; propagate dependency failures.
                        continue
                    else:
                        raise RecoveryRequiredError(
                            "No runnable tasks; saved state is inconsistent"
                        )
                    return
                completed, _ = await asyncio.wait(
                    {*active, cancel_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for future in completed:
                    if future is not cancel_waiter:
                        del active[future]
                        future.result()
        finally:
            cancel_waiter.cancel()
            await asyncio.gather(cancel_waiter, return_exceptions=True)
            await self._drain(active)

    @staticmethod
    async def _drain(active: dict[asyncio.Task[None], SimulationTask]) -> None:
        for future in active:
            future.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        active.clear()

    def _input(
        self,
        run: RunRecord,
        task: SimulationTask,
        results: dict[str, ToolResult],
    ) -> ToolInput:
        parameters = dict(task.parameters)
        parameters.update(
            {name: resolve(ref, run.request, results) for name, ref in task.bindings.items()}
        )
        # Stable across retry and recovery; independent of Python's randomized hash().
        digest = hashlib.sha256(f"{run.plan.config.seed}:{task.id}".encode()).digest()
        value = ToolInput(
            run_id=run.id,
            task_id=task.id,
            scenario=run.request.scenario,
            initial_state=run.request.initial_state,
            parameters=parameters,
            parameter_units=task.parameter_units,
            data=run.request.data,
            seed=int.from_bytes(digest[:4], "big"),
            versions={
                **run.request.versions,
                **run.plan.versions,
                f"tool:{task.tool}": task.tool_version,
            },
        )
        return self.validator.validate_input(task, value, run.request)

    async def _execute_task(
        self,
        run: RunRecord,
        task: SimulationTask,
        results: dict[str, ToolResult],
    ) -> None:
        record = run.tasks[task.id]
        tool = self.registry.get(task.tool)
        try:
            value = self._input(run, task, results)
        except (ValueError, KeyError, IndexError, PlanValidationError) as error:
            record.error = str(error)
            if record.state == TaskState.PENDING:
                record.state, record.finished_at = TaskState.FAILED, now()
            else:
                record.state = TaskState.INTERRUPTED
            self._save(run, f"task.{record.state.value}", task.id, error=record.error)
            return
        try:
            while True:
                if record.state == TaskState.PENDING:
                    record.attempt += 1
                    record.idempotency_key = f"{run.id}:{task.id}:{record.attempt}"
                    record.job_id = None
                    record.started_at = now()
                    record.deadline_at = now() + timedelta(seconds=task.timeout_seconds)
                    record.state, record.error = TaskState.SUBMITTING, None
                    self._save(
                        run,
                        "task.submitting",
                        task.id,
                        attempt=record.attempt,
                        idempotency_key=record.idempotency_key,
                        input=value.model_dump(mode="json"),
                    )
                elif not record.job_id and not (
                    tool.descriptor.idempotent and tool.descriptor.resumable
                ):
                    raise RecoveryRequiredError(
                        "Uncertain submissions require a durable tool with idempotent submission"
                    )
                elif not record.job_id:
                    # Reattach using the persisted key even if the original deadline expired.
                    # A durable backend must retain the key for the entire recovery window.
                    if record.idempotency_key is None:
                        raise RecoveryRequiredError("Saved submission key is missing")
                    async with asyncio.timeout(self.cancellation_timeout_seconds):
                        record.job_id = await tool.submit(
                            value,
                            idempotency_key=record.idempotency_key,
                        )
                    if not record.job_id:
                        raise RecoveryRequiredError("Tool returned an empty job ID")
                    record.state = TaskState.RUNNING
                    self._save(run, "task.reattached", task.id, job_id=record.job_id)
                try:
                    if record.deadline_at is None or record.idempotency_key is None:
                        raise RecoveryRequiredError(
                            "Saved attempt is missing its deadline or submission key"
                        )
                    remaining = (record.deadline_at - now()).total_seconds()
                    if remaining <= 0 and record.job_id:
                        # A detached job may have completed before the process resumed.
                        async with asyncio.timeout(self.cancellation_timeout_seconds):
                            update = await tool.status(record.job_id)
                            if update.job_id != record.job_id:
                                raise RecoveryRequiredError("Status returned a different job ID")
                            if update.state == JobState.SUCCEEDED:
                                output = await self._fetch_result(tool, record.job_id)
                            elif update.state == JobState.FAILED:
                                raise ToolExecutionError(
                                    update.message or "Tool job failed", retryable=update.retryable
                                )
                            else:
                                raise TimeoutError
                    else:
                        async with asyncio.timeout(max(0, remaining)):
                            if record.job_id is None:
                                record.job_id = await tool.submit(
                                    value,
                                    idempotency_key=record.idempotency_key,
                                )
                                if not record.job_id:
                                    raise RecoveryRequiredError("Tool returned an empty job ID")
                                record.state = TaskState.RUNNING
                                self._save(run, "task.submitted", task.id, job_id=record.job_id)
                            output = await self._poll(
                                tool,
                                record.job_id,
                                run.plan.config.poll_interval_seconds,
                            )
                    reference = self.store.put_result(output)
                    record.result_ref, record.state = reference, TaskState.SUCCEEDED
                    record.finished_at, record.error = now(), None
                    results[task.id] = output
                    self._save(run, "task.succeeded", task.id, result_ref=reference)
                    return
                except TimeoutError:
                    if not record.job_id or not await self._stop(tool, record.job_id):
                        raise RecoveryRequiredError(
                            "Task timed out; remote completion/cancellation is unknown"
                        ) from None
                    async with asyncio.timeout(self.cancellation_timeout_seconds):
                        stopped = await tool.status(record.job_id)
                    if stopped.job_id != record.job_id:
                        raise RecoveryRequiredError("Status returned a different job ID") from None
                    if stopped.state == JobState.SUCCEEDED:
                        raise RecoveryRequiredError(
                            "Job completed but result retrieval timed out; "
                            "resume to fetch its result"
                        ) from None
                    if stopped.state not in {JobState.FAILED, JobState.CANCELLED}:
                        raise RecoveryRequiredError("Job stop is not confirmed by status") from None
                    error = ToolExecutionError(
                        "Task deadline exceeded; job has stopped", retryable=True
                    )
                except ToolExecutionError as failure:
                    error = failure
                record.error = str(error)
                self._save(
                    run,
                    "task.attempt_failed",
                    task.id,
                    error=record.error,
                    attempt=record.attempt,
                    job_id=record.job_id,
                )
                if (
                    error.retryable
                    and tool.descriptor.idempotent
                    and record.attempt < task.retry.max_attempts
                ):
                    # Persist before sleeping; a crash here resumes the next attempt.
                    record.state, record.job_id = TaskState.PENDING, None
                    self._save(
                        run,
                        "task.retry_scheduled",
                        task.id,
                        delay_seconds=task.retry.delay(record.attempt),
                    )
                    await asyncio.sleep(task.retry.delay(record.attempt))
                    continue
                record.state, record.finished_at = TaskState.FAILED, now()
                self._save(run, "task.failed", task.id, error=record.error)
                return
        except asyncio.CancelledError:
            # Detach polling without pretending the underlying job was cancelled.
            if record.state not in TERMINAL_TASKS and record.state != TaskState.PENDING:
                record.state = TaskState.INTERRUPTED
                record.error = "Executor detached; job may still be running"
                self._save(run, "task.interrupted", task.id, error=record.error)
            raise
        except Exception as error:
            record.state, record.error = TaskState.INTERRUPTED, f"{type(error).__name__}: {error}"
            self._save(run, "task.interrupted", task.id, error=record.error)

    @classmethod
    async def _poll(cls, tool: ModelTool, job_id: str, interval: float) -> ToolResult:
        previous: JobState | None = None
        while True:
            update = await tool.status(job_id)
            if update.job_id != job_id:
                raise RecoveryRequiredError("Status response returned a different job ID")
            if update.state != previous:
                logger.debug("job=%s state=%s", job_id, update.state)
                previous = update.state
            if update.state == JobState.SUCCEEDED:
                return await cls._fetch_result(tool, job_id)
            if update.state == JobState.FAILED:
                raise ToolExecutionError(
                    update.message or "Tool job failed", retryable=update.retryable
                )
            if update.state == JobState.CANCELLED:
                raise ToolExecutionError(update.message or "Tool job was cancelled")
            await asyncio.sleep(interval)

    @staticmethod
    async def _fetch_result(tool: ModelTool, job_id: str) -> ToolResult:
        try:
            return tool.validate_result(await tool.result(job_id))
        except ValueError as error:
            raise ToolExecutionError(f"Invalid tool result: {error}") from error

    async def _stop(self, tool: ModelTool, job_id: str) -> bool:
        try:
            async with asyncio.timeout(self.cancellation_timeout_seconds):
                return await tool.cancel(job_id)
        except Exception:
            return False

    async def _cancel_records(self, run: RunRecord) -> None:
        for task in run.plan.tasks:
            record = run.tasks[task.id]
            if record.state in TERMINAL_TASKS:
                continue
            stopped = record.state == TaskState.PENDING
            if record.job_id:
                stopped = await self._stop(self.registry.get(task.tool), record.job_id)
            if stopped:
                record.state, record.finished_at = TaskState.CANCELLED, now()
                record.error = "Cancellation requested"
            else:
                record.state = TaskState.INTERRUPTED
                record.error = "Cannot confirm job cancellation"
            self._save(run, f"task.{record.state.value}", task.id, error=record.error)
