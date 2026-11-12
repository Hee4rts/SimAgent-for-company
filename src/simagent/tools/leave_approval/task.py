"""审批任务适配：供统一规划与执行框架使用的七个工具及结果引用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from functools import wraps
from threading import RLock
from datetime import date, datetime, time, timedelta
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator
from pydantic_ai import FunctionToolset, ModelRetry, RunContext, Tool

from ...infrastructure.dependencies import Services
from ...schemas.base import Contract
from . import api as core
from .models import (
    ApproverSnapshot, LeaveFacts, LeaveType, Name, NodeTiming, Nonnegative,
    OptimizationRequest, Positive, Probability, Provenance, QueuePool, QueueRequest,
    Reviewer, RoleBinding, SimulationCase, SimulationScenario, WorkCalendar,
    WorkflowDefinition, RouteResult, ResolvedApproval, QueueAnalysis,
    SimulationResult, OptimizationResult, ScenarioComparison,
)

TOOL_LABELS = {
    "get_leave_workflow": "读取审批流程",
    "calculate_leave_route": "计算审批路径",
    "resolve_leave_approvers": "解析审批人员",
    "analyze_approval_queues": "分析排队与积压",
    "simulate_leave_workflow": "运行流程仿真",
    "optimize_approval_capacity": "优化审批资源",
    "compare_approval_scenarios": "比较仿真方案",
}

Weekday = Literal["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

APPROVAL_INSTRUCTIONS = """
你为“审批电子流”任务生成 SimulationPlan，使用已接入的请假审批流程。
用户用自然语言描述条件，你负责整理参数、规划工具任务和追问必要信息；工具仅由执行器调用。
不要让用户编写 Python、运行本地示例、提供 JSON、寻找节点 ID 或复制工具返回内容。
七个工具使用当前会话内的 reference 引用；原始结果由服务端保存，你只传实际返回的引用。

收到仿真请求：规划 get_leave_workflow、calculate_leave_route、resolve_leave_approvers、
simulate_leave_workflow 的任务依赖链。若已有有效引用，可以从需要修改的步骤开始。
前序输出未执行，不能预先猜 reference。使用 bindings 绑定前序任务的 /reference，
例如 resolve_leave_approvers 的 route_ref 绑定计算路径任务，simulate_leave_workflow 的
people_ref 绑定人员任务。conditions、staffing 是静态 parameters；不得用字符串表达式引用结果。
始终以 input_schema 为准：calculate_leave_route 的单个模型参数会展平，直接传 payroll、
leave_types 等字段；simulate_leave_workflow 则使用 people_ref 和嵌套 conditions。
跨轮引用仅可使用 task_context.saved_results 中实际存在的 reference。
每个工具返回 kind、reference 以及展示字段。仿真指标位于 /summary/completed、
/summary/incomplete、/summary/completion_rate、/summary/sla_attainment_rate、
/summary/mean_cycle_minutes、/summary/p95_cycle_minutes。时长可能为 null；
至少为仿真计划定义完成量、未完成量、完成率和 SLA 达标率四项程序指标。
时长非空才可作为数值指标，未提供成本时不定义费用指标。查看流程的计划可以没有指标。
优化结果候选仿真的引用在 /candidate_ref，比较工具使用两个 simulation 引用。
比较输出 changes 是指标差值数组，不能把它当仿真 summary。
工具返回 needs_data 时仅询问缺少的业务事实，不编造 payroll、年度累计、证明病假、医疗期、
外派判断、HRBP 抄送规则、申请量、人数或处理时长。可把多个必要问题合并一次询问。
用户只问流程时读取流程即可；无需强行调用全部七个工具。要求优化时调用优化工具；
要求比较时取得两个真实仿真结果后调用比较工具。分析泊松稳态时才使用排队工具，
不要把批量同时到达的仿真与稳态排队模型混为一谈。

当前原始流程的9个角色为 ATTENDANCE_CLERK 考勤专员、HEALTH_GUIDANCE_CENTER 健康指导中心、
DIRECT_MANAGER 直接主管、SUBSIDIARY_OWNER 子公司负责人、UPPER_ORG_MANAGER 上一层主管、
EMPLOYEE_SUPPORT_TEAM 员工保障小组、L1_HR_DEPT_HEAD 人力资源部长、L1_ORG_PRESIDENT 一层总裁、
COMPANY_HR_PRESIDENT CHR总裁。自动审批角色不要求人工人数或处理时长。
常见假别：事假 PERSONAL_LEAVE、病假 SICK_LEAVE、年休假 PAID_ANNUAL_LEAVE、产假 MATERNITY_LEAVE。

人数输入可生成仅用于仿真的人员标识；用户说明同一人处理多个节点时必须复用 employee_ids，
不能把一个人重复算成多份能力。只提供人数时说明按各角色独立人员池测算。
申请量为同类规则事实下的批次，每单使用相同角色候选和节点耗时。混合假别/组织应分别建场景。
arrival_interval_minutes=0 仅表示同时到达；均匀到达应根据用户给定速率换算。
arrival_minutes 从起始日期00:00计数，9点是540分钟；没有修改首次到达时刻时省略该字段。
关于到达方式有歧义时询问，不能暗设所有申请同时到达。
可用默认条件：周一至周五9–17点、观察7个自然日、总时长SLA24小时、固定服务时长、
不发生驳回、seed=0。首次计算说明这些缺省条件，并允许用户修改。
没有提供成本时不能把0说成免费；若用户要成本优化先询问人员小时成本和预算。

多轮修改：沿用用户已给出的条件，改动明确指定的参数，重新运行相关工具。
比较人数时先解析包含两方案候选人的统一候选集，再用 active_employee_ids 分别启用人员。
若上一场景候选不够，重新解析扩充候选集，然后在同一引用上重新跑基线和新方案。
两方案必须保持同一申请群体、事实和观察口径；不得伪造比较结果。
优化只在给定合格候选中启停人员；不得通过删审批节点或放宽规则满足目标。

当前不支持修改审批阈值、混合申请批次、随机高峰到达、统计学习估参或错批风险模型。
用户请求这些能力时明确说明边界，不得用修改请假事实或固定间隔冒充对应能力。
"""

APPROVAL_INTERPRETATION = """
回答中文，summary 用简短结论说明发现。页面只展示最终回答，需在回答中提供用户要求的指标，
比较多方案时可用简短的 Markdown 表格，不要让用户寻找工具卡片或执行记录。
优先报告完成率、平均/P95时长、SLA、积压与瓶颈，明确未完成申请和假设。
若路径或人员结果为 needs_data，在 recommendations 中列出需要用户补充的信息，不能称仿真已完成。
节点 mean_wait_minutes 是每次办理的平均等待，不是等待总和或最大值；summary.rework_limit
是达到重提上限的申请数量，不是配置的次数上限。P95是插值分位数，不必等于最慢一单。
SLA针对到达到完成的总时长，不针对单独排队等待。不要推测工具未返回的逐单时间线。
同一指标不在多处反复罗列，不要输出 reference、内部参数名或框架步骤。
自动审批和通知只描述模拟中的流程动作，不得声称已完成真实审批或已发出邮件。
所有结论必须来自成功工具返回；结果来自用户条件下的情景测算，不声称已连接真实业务平台。
"""


def today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


@dataclass
class ApprovalWorkspace:
    version: str = field(default_factory=lambda: uuid4().hex)
    artifacts: dict = field(default_factory=dict)
    cache: dict = field(default_factory=dict)
    on_change: Callable[[], None] | None = field(default=None, repr=False)
    lock: RLock = field(default_factory=RLock, repr=False)

    def put(self, kind, value, display, **extras):
        with self.lock:
            reference = f"{kind}_{uuid4().hex[:12]}"
            self.artifacts[reference] = {"kind": kind, "value": value, "display": display, **extras}
            if self.on_change:
                self.on_change()
            return {"reference": reference, "kind": kind, **display}

    def get(self, reference, kind):
        with self.lock:
            artifact = self.artifacts.get(reference)
            if not artifact or artifact["kind"] != kind:
                raise ModelRetry("当前任务中没有该计算结果，或引用已过期。请先重新调用前序工具。")
            return artifact

    def context(self):
        with self.lock:
            return {"today": str(today()), "saved_results": [
                {"reference": key, "kind": item["kind"], **item["display"],
                 "conditions": item["conditions"].model_dump(mode="json") if item.get("conditions") else None,
                 "people_ref": item.get("people_ref")}
                for key, item in list(self.artifacts.items())[-12:]
            ]}

    def dump(self):
        def encode(value):
            if isinstance(value, BaseModel):
                return {"model_type": type(value).__name__, "document": value.model_dump(mode="json")}
            if isinstance(value, dict):
                return {key: encode(item) for key, item in value.items()}
            if isinstance(value, list):
                return [encode(item) for item in value]
            return value
        with self.lock:
            return {"version": self.version, "artifacts": encode(self.artifacts), "cache": self.cache.copy()}

    @classmethod
    def restore(cls, document):
        models = {model.__name__: model for model in [WorkflowDefinition, RouteResult, ResolvedApproval,
                  QueueAnalysis, SimulationResult, OptimizationResult, ScenarioComparison,
                  Reviewer, SimulationScenario, SimulationConditions]}
        def decode(value):
            if isinstance(value, dict):
                if set(value) == {"model_type", "document"} and value["model_type"] in models:
                    return models[value["model_type"]].model_validate(value["document"])
                return {key: decode(item) for key, item in value.items()}
            if isinstance(value, list):
                return [decode(item) for item in value]
            return value
        return cls(version=document["version"], artifacts=decode(document.get("artifacts", {})),
                   cache=document.get("cache", {}))


def workspace(ctx):
    return ctx.deps.clients["approval_workspace"]


def invoke(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ValueError as error:
        raise ModelRetry(str(error)) from None


def provenance(ws):
    return Provenance(source="user_provided", version=ws.version)


class LeaveConditions(Contract):
    payroll: str
    leave_types: list[LeaveType] = Field(min_length=1, max_length=16)
    annual_sick_days: Nonnegative | None = None
    annual_personal_days: Nonnegative | None = None
    sick_certificate: Literal["certified", "uncertified"] | None = None
    medical_period_exceeded: bool | None = None
    health_check_required: bool | None = None
    subsidiary_rule_matched: bool | None = None
    notify_hrbp: bool | None = None
    department_code: Name = "simulation-department"
    applicant_id: Name = "simulation-applicant"


class RoleStaffing(Contract):
    role_code: Name
    count: int | None = Field(default=None, ge=1, le=20)
    employee_ids: list[Name] | None = Field(default=None, min_length=1, max_length=20)
    hourly_cost: Nonnegative | None = None

    @model_validator(mode="after")
    def choice(self):
        if (self.count is None) == (self.employee_ids is None):
            raise ValueError("人数 count 和具体人员 employee_ids 必须提供其中一项")
        if self.employee_ids and len(set(self.employee_ids)) != len(self.employee_ids):
            raise ValueError("同一角色的人员标识不能重复")
        return self


class RoleTiming(Contract):
    role_code: Name
    mean_service_minutes: Positive
    distribution: Literal["fixed", "exponential"] = "fixed"
    reject_probability: Probability = 0


class SimulationConditions(Contract):
    name: Name
    case_count: int = Field(ge=1, le=200)
    arrival_interval_minutes: Nonnegative
    timings: list[RoleTiming] = Field(min_length=1, max_length=9)
    active_employee_ids: list[Name] | None = Field(default=None, min_length=1, max_length=100)
    start_date: date | None = None
    start_hour: int = Field(default=9, ge=0, le=23)
    end_hour: int = Field(default=17, ge=1, le=24)
    weekdays: list[Weekday] = Field(default_factory=lambda: WEEKDAYS[:5], min_length=1, max_length=7,
                                  description="工作日名称；默认周一至周五。")
    arrival_minutes: Nonnegative | None = Field(default=None, description="从 start_date 的00:00计数；9点=540。省略时默认工作开始时刻，不能把工作开始误写成0。")
    horizon_minutes: Positive = Field(default=10080, le=527040,
                                      description="观察结束时刻，从 start_date 的00:00计数；当天17点=1020分钟。")
    sla_minutes: Positive = 1440
    resubmit_delay_minutes: Nonnegative = 60
    max_rework: int = Field(default=3, ge=0, le=10)
    seed: int = Field(default=0, ge=0, le=2**32-1)


def route_display(route):
    labels = {
        "annual_sick_days": "含本次申请的年度累计病假天数", "annual_personal_days": "含本次申请的年度累计事假天数",
        "sick_certificate": "病假是否有证明", "medical_period_exceeded": "是否超过医疗期",
        "health_check_required": "是否要求健康核检", "subsidiary_rule_matched": "是否命中外派子公司审批规则",
        "notify_hrbp": "是否需要抄送 HRBP",
    }
    return {"status": route.status, "title": "请假审批路径", "steps": [
        {"name": s.name, "role_code": s.role_code, "automatic": s.auto_approve, "reason": s.reason}
        for s in route.steps], "notifications": route.notifications,
        "missing": [labels.get(k, k) for k in route.missing_fields]}


def summary_display(run, cost_known):
    summary = run.summary.model_dump(mode="json")
    if not cost_known:
        summary["staffing_cost"] = None
    names = {n.id: n.name for n in core.get_leave_workflow().nodes}
    return {"title": run.name, "summary": summary,
            "bottlenecks": [{"name": names.get(n.node_id, n.node_id), "mean_wait_minutes": n.mean_wait_minutes,
                             "handled": n.handled_visits} for n in sorted(run.nodes, key=lambda x: x.mean_wait_minutes, reverse=True)],
            "resources": [{"name": r.employee_id, "utilization": r.utilization} for r in run.reviewers]}


def get_leave_workflow(ctx: RunContext[Services]) -> dict:
    """读取当前审批任务的流程、审批角色和条件；无需用户提供文件路径。"""
    flow = core.get_leave_workflow()
    return workspace(ctx).put("workflow", flow, {
        "title": flow.name, "version": flow.version,
        "steps": [{"name": n.name, "role_code": n.role_code, "mode": n.sign_mode}
                  for n in flow.nodes if n.kind == "approval"],
        "note": "9 个审批节点按业务条件进入；读取流程不表示实际申请会经过全部节点。",
    })


def calculate_leave_route(ctx: RunContext[Services], conditions: LeaveConditions) -> dict:
    """将用户给出的请假条件代入路径规则；未知业务事实保留 null，返回需补充的信息和 route 引用。"""
    ws = workspace(ctx)
    facts = LeaveFacts(**conditions.model_dump(), provenance=provenance(ws), application_id="web-prototype")
    route = invoke(core.calculate_leave_route, facts)
    return ws.put("route", route, route_display(route))


def resolve_leave_approvers(
    ctx: RunContext[Services], route_ref: str, staffing: list[RoleStaffing], as_of: date | None = None,
) -> dict:
    """按用户提供的角色人数或人员解析审批候选，返回 people 引用。人数会生成仿真人员标识；跨角色同一人需复用 employee_ids。"""
    ws = workspace(ctx)
    route = ws.get(route_ref, "route")["value"]
    if route.status != "ready":
        raise ModelRetry("请先补齐请假事实并取得 ready 的审批路径。")
    if len({s.role_code for s in staffing}) != len(staffing):
        raise ModelRetry("同一审批角色只配置一次人数或人员。")
    roles = {s.role_code for s in route.steps if not s.auto_approve}
    if any(s.role_code not in roles for s in staffing):
        raise ModelRetry("只需配置当前路径上的人工审批角色，自动或未经过的节点不需要人员。")
    bindings, reviewers, costs = [], {}, {}
    for config in staffing:
        ids = config.employee_ids or [f"{config.role_code.lower()}-{i+1}" for i in range(config.count)]
        bindings.append(RoleBinding(role_code=config.role_code, department_code=route.department_code, employee_ids=ids))
        for person in ids:
            if person in costs and costs[person] is not None and config.hourly_cost is not None and costs[person] != config.hourly_cost:
                raise ModelRetry("同一人员在不同角色的小时成本必须一致。")
            costs[person] = config.hourly_cost if config.hourly_cost is not None else costs.get(person)
            reviewers[person] = Reviewer(employee_id=person, hourly_cost=costs[person] or 0)
    snapshot = ApproverSnapshot(provenance=provenance(ws), as_of=as_of or today(), bindings=bindings)
    resolved = invoke(core.resolve_leave_approvers, route, snapshot)
    return ws.put("people", resolved, {
        "title": "审批人员配置", "status": resolved.status,
        "roles": [{"name": s.name, "role_code": s.role_code, "automatic": s.auto_approve,
                   "employee_ids": s.eligible_ids} for s in resolved.steps],
        "missing_roles": resolved.missing_roles,
        "assumptions": ["人数模式按各角色独立人员池测算；相同人员标识代表共享处理能力。",
                        "人员是用户为本次情景指定的合格候选，未连接真实组织目录。"],
    }, reviewers=list(reviewers.values()), costs=costs)


def analyze_approval_queues(ctx: RunContext[Services], pools: list[QueuePool]) -> dict:
    """对用户给出的独立人员池进行稳态排队分析。到达率单/小时，实际处理与等待目标为分钟；批量同时到达请调用流程仿真。"""
    ws = workspace(ctx)
    result = invoke(core.analyze_approval_queues, QueueRequest(provenance=provenance(ws), pools=pools))
    return ws.put("queue", result, {"title": "排队与积压分析", "pools": [p.model_dump(mode="json") for p in result.pools],
                                    "assumptions": result.assumptions})


def simulate_leave_workflow(ctx: RunContext[Services], people_ref: str, conditions: SimulationConditions) -> dict:
    """根据人员引用和用户的批量申请条件调用流程仿真，返回 simulation 引用与指标。修改条件后再次调用即可。

    arrival_interval_minutes=0 表示同时到达，非零为固定间隔；不得从含糊的申请量擅自推断。
    active_employee_ids 可只启用候选全集的一部分，便于在同一资格口径下比较不同人数。
    """
    ws = workspace(ctx)
    artifact = ws.get(people_ref, "people")
    resolved = artifact["value"]
    if resolved.status != "ready":
        raise ModelRetry("人工审批节点的人员尚未配齐，请先补充人员配置。")
    timings = {t.role_code: t for t in conditions.timings}
    if len(timings) != len(conditions.timings):
        raise ModelRetry("同一角色的处理时长只能配置一次。")
    missing = [s.name for s in resolved.steps if not s.auto_approve and s.role_code not in timings]
    if missing:
        raise ModelRetry("还缺少这些人工节点的实际处理时长：" + "、".join(missing))
    all_ids = {r.employee_id for r in artifact["reviewers"]}
    active = conditions.active_employee_ids
    if active is not None and (len(set(active)) != len(active) or not set(active) <= all_ids):
        raise ModelRetry("启用人员必须来自本次解析返回的候选人员，且不能重复。")
    reviewers = [r for r in artifact["reviewers"] if active is None or r.employee_id in active]
    cases = []
    arrival = conditions.arrival_minutes if conditions.arrival_minutes is not None else conditions.start_hour * 60
    for i in range(conditions.case_count):
        # ID 由用户事实和批次序号确定，人数配置改变时仍可使用相同申请群体重算。
        facts = resolved.route.facts.model_copy(update={"application_id": f"web-case-{i+1}"})
        approval = core.resolve_leave_approvers(core.calculate_leave_route(facts), resolved.snapshot)
        cases.append(SimulationCase(
            case_id=facts.application_id, arrival_minutes=arrival + i * conditions.arrival_interval_minutes,
            approval=approval, resubmit_delay_minutes=conditions.resubmit_delay_minutes,
            timings=[NodeTiming(node_id=s.node_id, **timings[s.role_code].model_dump(exclude={"role_code"}))
                     for s in approval.steps if not s.auto_approve],
        ))
    try:
        scenario = SimulationScenario(
            name=conditions.name, provenance=provenance(ws),
            calendar=WorkCalendar(start_date=conditions.start_date or resolved.snapshot.as_of,
                                  weekdays=[WEEKDAYS.index(day) for day in conditions.weekdays],
                                  start_hour=conditions.start_hour, end_hour=conditions.end_hour),
            horizon_minutes=conditions.horizon_minutes, sla_minutes=conditions.sla_minutes, seed=conditions.seed,
            max_rework=conditions.max_rework, cases=cases, reviewers=reviewers,
        )
    except ValueError as error:
        raise ModelRetry(str(error)) from None
    run = invoke(core.simulate_leave_workflow, scenario)
    cost_known = all(artifact["costs"][r.employee_id] is not None for r in reviewers)
    midnight = datetime.combine(scenario.calendar.start_date, time())
    first_arrival = (midnight + timedelta(minutes=arrival)).strftime("%Y-%m-%d %H:%M")
    observation_end = (midnight + timedelta(minutes=conditions.horizon_minutes)).strftime("%Y-%m-%d %H:%M")
    assumptions = [
        f"{conditions.case_count} 张同类申请，首次于 {first_arrival} 到达；" + ("同时到达。" if conditions.arrival_interval_minutes == 0 else f"随后间隔 {conditions.arrival_interval_minutes:g} 分钟到达。"),
        f"工作时间 {conditions.start_hour}:00–{conditions.end_hour}:00，工作日为{'、'.join(conditions.weekdays)}。",
        f"观察至 {observation_end}；总时长 SLA 为 {conditions.sla_minutes/60:g} 小时。",
        f"本次启用 {len(reviewers)} 名候选人员；随机种子为 {conditions.seed}。",
        "基于用户条件的情景测算，未连接真实业务数据；时长均值/P95只统计已完成申请。",
    ]
    for step in resolved.steps:
        if not step.auto_approve:
            timing = timings[step.role_code]
            service = "固定时长" if timing.distribution == "fixed" else "指数分布均值"
            assumptions.append(f"{step.name}：{service} {timing.mean_service_minutes:g} 分钟，驳回概率 {timing.reject_probability:.0%}。")
    if not cost_known:
        assumptions.append("未提供完整人员成本，本次不展示费用结论。")
    return ws.put("simulation", run, {**summary_display(run, cost_known), "assumptions": assumptions},
                  scenario=scenario, people_ref=people_ref, conditions=conditions, cost_known=cost_known)


def optimize_approval_capacity(
    ctx: RunContext[Services], simulation_ref: str, budget: Nonnegative,
    minimum_sla_attainment: Probability = 0.95, minimum_completion_rate: Probability = 1,
    solver: Literal["differential_evolution", "enumeration"] = "differential_evolution",
) -> dict:
    """对已仿真场景的全部合格候选人员优化启用子集。必须有人员小时成本和预算；不会自动创造新增资格。"""
    ws = workspace(ctx)
    artifact = ws.get(simulation_ref, "simulation")
    people = ws.get(artifact["people_ref"], "people")
    if any(v is None for v in people["costs"].values()):
        raise ModelRetry("优化前请询问并配置所有候选人员的小时成本，不能将未知成本当作免费。")
    roster = people["reviewers"]
    if len(roster) > 10:
        raise ModelRetry("网页资源优化最多支持 10 个可启停候选人，请缩小候选集；仿真可使用更多人员。")
    scenario = artifact["scenario"].model_copy(update={"reviewers": roster})
    request = OptimizationRequest(scenario=scenario, candidate_ids=[r.employee_id for r in roster], budget=budget,
                                  minimum_sla_attainment=minimum_sla_attainment, minimum_completion_rate=minimum_completion_rate,
                                  solver=solver, validation_seed=(scenario.seed + 314159) % 2**32)
    result = invoke(core.optimize_approval_capacity, request)
    candidate = None
    if result.training:
        selected_scenario = scenario.model_copy(update={"reviewers": [r for r in roster if r.employee_id in result.selected_ids]})
        candidate = ws.put("simulation", result.training, summary_display(result.training, True),
                           scenario=selected_scenario, people_ref=artifact["people_ref"], cost_known=True)
    return ws.put("optimization", result, {
        "title": "资源优化结果", "status": result.status, "selected_ids": result.selected_ids,
        "evaluations": result.evaluations, "violations": result.violations,
        "candidate_ref": candidate["reference"] if candidate else None,
        "summary": result.training.summary.model_dump(mode="json") if result.training else None,
        "validation": result.validation.summary.model_dump(mode="json") if result.validation else None,
        "assumptions": ["在已配置的合格候选中选择启用人员，保留原审批路径。", "搜索方案已用独立种子复核，未声明全局最优。"],
    })


def compare_approval_scenarios(ctx: RunContext[Services], baseline_ref: str, candidate_ref: str) -> dict:
    """用两个 simulation 引用比较真实计算结果；须保持同一申请群体和候选资格口径，不能手填指标。"""
    ws = workspace(ctx)
    baseline = ws.get(baseline_ref, "simulation")
    candidate = ws.get(candidate_ref, "simulation")
    result = invoke(core.compare_approval_scenarios, baseline["value"], candidate["value"])
    changes = [c.model_dump(mode="json") for c in result.changes
               if c.metric != "staffing_cost" or baseline["cost_known"] and candidate["cost_known"]]
    return ws.put("comparison", result, {"title": "方案对比", "comparable": result.comparable,
        "changes": changes, "diagnostics": result.diagnostics,
        "baseline_name": baseline["value"].name, "candidate_name": candidate["value"].name})


def _planned_tool(function):
    @wraps(function)
    def execute(ctx: RunContext[Services], *args, **kwargs):
        ws = workspace(ctx)
        key = ctx.deps.idempotency_key
        with ws.lock:
            if key and key in ws.cache:
                return ws.cache[key]
        result = function(ctx, *args, **kwargs)
        with ws.lock:
            if key:
                ws.cache[key] = result
                if ws.on_change:
                    ws.on_change()
            return result
    return execute


def approval_toolset() -> FunctionToolset[Services]:
    toolset = FunctionToolset[Services]()
    for function in [get_leave_workflow, calculate_leave_route, resolve_leave_approvers,
                     analyze_approval_queues, simulate_leave_workflow, optimize_approval_capacity,
                     compare_approval_scenarios]:
        units = {
            "/summary/completed": "count", "/summary/incomplete": "count",
            "/summary/completion_rate": "ratio", "/summary/sla_attainment_rate": "ratio",
            "/summary/mean_cycle_minutes": "min", "/summary/p95_cycle_minutes": "min",
        } if function is simulate_leave_workflow else {}
        toolset.add_tool(Tool(_planned_tool(function), sequential=True, max_retries=2,
                             metadata={"simagent": {"version": "1", "idempotent": True,
                                       "tags": ["leave_approval"], "output_units": units}}))
    return toolset
