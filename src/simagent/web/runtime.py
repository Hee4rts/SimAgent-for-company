"""Presentation adapter for the existing plan/execute/evaluate/interpret framework."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace

from pydantic_ai import FunctionToolset, RunContext, Tool
from pydantic_ai.usage import RunUsage
from pydantic_core import to_jsonable_python

from ..agents import AgentLimits, PlanSolveAgent, ResultInterpreter
from ..engine.executor import SimulationExecutor
from ..engine.validation import PlanValidator
from ..engine.workflow import SimulationFramework
from ..schemas import BusinessInput, RunEvent
from ..tools.leave_approval.task import APPROVAL_INSTRUCTIONS, APPROVAL_INTERPRETATION, TOOL_LABELS

STAGES = {
    "planning": "理解需求与生成计划", "validating": "校验计划与参数",
    "executing": "执行仿真任务", "evaluating": "计算指标与评估目标",
    "replanning": "根据执行反馈修订计划", "interpreting": "解释结果与关联证据",
    "completed": "处理完成", "needs_input": "等待补充条件", "discussed": "任务说明",
    "interrupted": "运行中断", "failed": "处理未完成", "cancelled": "已停止",
}

WEB_PLANNING = """
你通过企业仿真工作台与用户交互，回答中文。业务执行必须通过 SimulationPlan 交给执行器。
将用户的业务目标、边界、参数来源和任务依赖写进计划。需要工具执行的事实不可由语言回答代替。
只使用当前工具目录；没有工具支持时说明能力边界。不要声称已发送邮件或执行真实业务审批。
保留当前用户的条件；更改条件需要来自用户输入或明确列出的允许默认值。
解释参数缺失时使用业务名称，不要求用户编写 JSON、Python 或提供内部引用。
"""

WEB_INTERPRETATION = """
回答中文。summary 直接说明是否完成用户目标；findings 写有工具证据支持的结论。
已完成的工具不等于用户目标达成，必须结合程序评估和运行状态。
网页只展示最终回答，不展示工具卡片或运行步骤。回答应覆盖用户要求的关键指标和比较结果，
不要省略数字并让用户去看不存在的卡片。多项指标比较可以使用简短的 Markdown 表格。
不要暴露内部引用、智能体名称、执行步骤或粘贴完整 JSON；证据关联留在结构化结果中。
不要把缺失值解释为0。结果需要用户补充数据时明确指出；不编造未执行任务的结果。
"""


async def execution_toolset(toolsets, services, model):
    """Keep native tools; adapt API/MCP toolsets to the executor's function interface."""
    combined = FunctionToolset()
    ctx = RunContext(deps=services, model=model, usage=RunUsage())
    for source in toolsets:
        if isinstance(source, FunctionToolset):
            for tool in source.tools.values():
                combined.add_tool(tool)
            continue
        for name, published in (await source.get_tools(ctx)).items():
            def adapter(remote, tool_name):
                async def call(ctx, **parameters):
                    available = await remote.get_tools(ctx)
                    tool = available.get(tool_name)
                    if tool is None or tool.tool_def.kind != "function":
                        raise ValueError("工具当前不可用，或需要单独授权流程")
                    args = tool.args_validator.validate_json(json.dumps(to_jsonable_python(parameters), allow_nan=False))
                    if tool.args_validator_func is not None:
                        checked = tool.args_validator_func(ctx, **args)
                        if inspect.isawaitable(checked):
                            await checked
                    return await remote.call_tool(tool_name, args, ctx, tool)
                return call
            definition = published.tool_def
            tool = Tool.from_schema(adapter(source, name), name=name, description=definition.description,
                                    json_schema=definition.parameters_json_schema, takes_ctx=True,
                                    sequential=definition.sequential)
            tool.function_schema.validator = published.args_validator
            tool.metadata = definition.metadata
            tool.requires_approval = definition.kind != "function"
            combined.add_tool(tool)
    return combined


def run_view(run, store):
    tasks = []
    for task in run.plan.tasks:
        record = run.tasks[task.id]
        item = {"id": f"{run.id}:{task.id}", "task_id": task.id, "name": task.tool,
                "label": TOOL_LABELS.get(task.tool, task.description), "description": task.description,
                "depends_on": task.depends_on, "state": record.state.value,
                "attempt": record.attempt, "job_id": record.job_id,
                "status": {"succeeded": "completed", "failed": "failed", "skipped": "skipped",
                           "cancelled": "stopped", "interrupted": "interrupted", "pending": "pending"}.get(record.state.value, "running")}
        if record.state.value == "pending" and record.attempt:
            item["status"] = "retrying"
        if record.result_ref:
            result = store.get_result(record.result_ref)
            item["result"] = result.payload
        tasks.append(item)
    evaluation = None
    if run.evaluation:
        evaluation = {**run.evaluation.model_dump(mode="json"), "passed": run.evaluation.passed,
                      "labels": {m.name: m.description for m in run.plan.metrics}}
    return {"id": run.id, "parent_run_id": run.parent_run_id, "state": run.state.value,
            "plan": run.plan.model_dump(mode="json"), "tasks": tasks,
            "evaluation": evaluation,
            "interpretation": run.interpretation.model_dump(mode="json") if run.interpretation else None}


class ObservedRunStore:
    """Forward durable writes to the existing store before notifying the UI."""
    def __init__(self, store, on_record):
        self.store, self.on_record = store, on_record

    def create(self, run):
        self.store.create(run)
        self.on_record(run, RunEvent(kind="run.created"))

    def save(self, run, event=None):
        self.store.save(run, event)
        self.on_record(run, event)

    def load(self, run_id):
        return self.store.load(run_id)

    def put_result(self, result):
        return self.store.put_result(result)

    def get_result(self, reference):
        return self.store.get_result(reference)

    def events(self, run_id):
        return self.store.events(run_id)

    def exclusive(self, run_id):
        return self.store.exclusive(run_id)


class FrameworkRuntime:
    def __init__(self, *, model, tools, services, storage, thread, mode, timeout):
        self.thread, self.storage = thread, storage
        self.emit = lambda event: None
        self.current_run_id = None
        self.stop_requested = False
        self.services = replace(services, clients={**services.clients,
            "approval_workspace": thread.workspace, "planning_context": thread.workspace.context})
        limits = AgentLimits(timeout_seconds=timeout)
        validator = PlanValidator(tools)
        self.planner = PlanSolveAgent(model, tools, validator=validator, services=self.services, limits=limits,
                                     instructions=WEB_PLANNING + (APPROVAL_INSTRUCTIONS if mode == "approval" else ""))
        self.interpreter = ResultInterpreter(model, limits=limits,
                            instructions=WEB_INTERPRETATION + (APPROVAL_INTERPRETATION if mode == "approval" else ""))
        self.executor = SimulationExecutor(tools, ObservedRunStore(storage, self.on_record),
                                           validator=validator, services=self.services, execution_model=model)
        self.framework = SimulationFramework(self.planner, self.executor, self.interpreter, observer=self.on_stage)

    def on_stage(self, stage, details):
        if self.stop_requested:
            raise asyncio.CancelledError
        self.emit({"type": "stage", "stage": stage, "label": STAGES[stage], **details})

    def on_record(self, run, event):
        self.current_run_id = run.id
        if run.id not in self.thread.run_ids:
            self.thread.run_ids.append(run.id)
        self.emit({"type": "run", "run": {"id": run.id, "state": run.state.value},
                   "event": event.kind if event else None})

    async def run(self, message):
        self.current_run_id = None
        self.stop_requested = False
        request = BusinessInput(question=message, goals=[message],
            scenario={"conversation": [
                {"role": item["role"], "content": item["content"], "status": item.get("status", "completed")}
                for item in self.thread.messages[:-2][-20:]
            ]}, versions={"workspace": self.thread.workspace.version})
        return await self.framework.run(request)

    async def resume(self, run_id):
        self.stop_requested = False
        self.current_run_id = run_id
        self.on_stage("executing", {"run_id": run_id})
        return await self.framework.resume(run_id)

    async def cancel(self):
        self.stop_requested = True
        if self.current_run_id:
            await self.executor.cancel(self.current_run_id)


def response_text(result):
    if result.reply:
        return "\n\n".join([result.reply.message, *[f"• {q}" for q in result.reply.questions]])
    if result.interpretation:
        value = result.interpretation
        parts = [value.summary, *[finding.statement for finding in value.findings]]
        if value.tradeoffs:
            parts.append("方案权衡：" + "；".join(value.tradeoffs))
        if value.recommendations:
            parts.append("建议：" + "；".join(value.recommendations))
        if value.limitations:
            parts.append("适用条件：" + "；".join(value.limitations))
        return "\n\n".join(parts)
    return "运行尚未完成，已保存计划和执行记录。请查看任务状态，补充条件或继续处理。"
