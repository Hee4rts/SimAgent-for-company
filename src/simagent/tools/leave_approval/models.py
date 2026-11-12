"""工具的显式输入输出契约；时间统一使用分钟，排队到达率使用单/小时。"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ...schemas.base import Contract

Nonnegative = Annotated[float, Field(ge=0)]
Positive = Annotated[float, Field(gt=0)]
Probability = Annotated[float, Field(ge=0, le=1)]
Name = Annotated[str, Field(min_length=1, max_length=200)]


class Provenance(Contract):
    source: Literal["demo", "user_provided", "platform_snapshot"]
    version: Name


class Condition(Contract):
    flag: str
    equals: bool


class Branch(Condition):
    target_id: str


class WorkflowNode(Contract):
    id: str
    name: str
    kind: str
    next_ids: list[str]
    branches: list[Branch]
    role_code: str | None
    sign_mode: str | None
    skip_same_handler: bool
    auto_condition: Condition | None
    reject_to: list[str]
    withdraw_to: list[str]
    operations: list[str]
    function_name: str | None


class WorkflowDefinition(Contract):
    workflow_id: str
    name: str
    version: str
    source_sha256: str
    nodes: list[WorkflowNode]
    assumptions: list[str]


LeaveType = Literal[
    "PERSONAL_LEAVE", "SICK_LEAVE", "MATERNITY_LEAVE", "NURSING_LEAVE",
    "BEREAVEMENT_LEAVE", "PAID_ANNUAL_LEAVE", "OVERSEAS_ANNUAL_LEAVE",
    "WEEKEND_COMPENSATORY_LEAVE", "PHYSICAL_EXAMINATION_LEAVE",
    "PRENATAL_EXAMINATION_LEAVE", "FAMILY_PLANNING_LEAVE", "PREMARITAL_EXAMINATION_LEAVE",
    "MARRIAGE_LEAVE", "PATERNITY_LEAVE", "ABORTION_LEAVE", "WORK_INJURY_LEAVE",
]


class LeaveFacts(Contract):
    """已按原规则核算的事实；年度累计含本单、有效历史和待汇总数据，跨年取最大值。"""

    provenance: Provenance
    application_id: Name
    applicant_id: Name
    department_code: Name
    payroll: Name
    leave_types: list[LeaveType] = Field(min_length=1, max_length=16)
    annual_sick_days: Nonnegative | None = None
    annual_personal_days: Nonnegative | None = None
    sick_certificate: Literal["certified", "uncertified"] | None = None
    medical_period_exceeded: bool | None = None
    health_check_required: bool | None = None
    subsidiary_rule_matched: bool | None = None
    notify_hrbp: bool | None = None


class ApprovalStep(Contract):
    node_id: str
    name: str
    role_code: str
    auto_approve: bool
    skip_same_handler: bool
    reason: str


class RouteResult(Contract):
    status: Literal["ready", "needs_data", "not_applicable"]
    provenance: Provenance
    application_id: str
    applicant_id: str
    department_code: str
    workflow_version: str
    workflow_sha256: str
    facts: LeaveFacts
    flags: dict[str, bool] = Field(default_factory=dict)
    steps: list[ApprovalStep] = Field(default_factory=list)
    notifications: list[str] = Field(default_factory=list)
    visited_nodes: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class RoleBinding(Contract):
    """平台组织规则解析后的角色/部门候选集；此处不重新猜测组织层级。"""

    role_code: Name
    department_code: Name
    employee_ids: list[Name] = Field(max_length=100)


class Delegation(Contract):
    authorizer_id: Name
    delegate_ids: list[Name] = Field(min_length=1, max_length=100)
    department_code: Name
    applicant_id: Name | None = None
    valid_from: date
    valid_until: date

    @model_validator(mode="after")
    def dates(self):
        if self.valid_until < self.valid_from:
            raise ValueError("授权截止日期不能早于起始日期")
        return self


class ApproverSnapshot(Contract):
    provenance: Provenance
    as_of: date
    bindings: list[RoleBinding] = Field(max_length=500)
    delegations: list[Delegation] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def unique(self):
        keys = [(b.role_code, b.department_code) for b in self.bindings]
        if len(set(keys)) != len(keys):
            raise ValueError("同一角色和部门只能提供一个已经合并的候选集")
        return self


class ResolvedStep(ApprovalStep):
    eligible_ids: list[str]


class ResolvedApproval(Contract):
    status: Literal["ready", "needs_data", "not_applicable"]
    route: RouteResult
    snapshot: ApproverSnapshot
    steps: list[ResolvedStep] = Field(default_factory=list)
    missing_roles: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class QueuePool(Contract):
    pool_id: Name
    arrival_per_hour: Nonnegative
    mean_service_minutes: Positive
    servers: int = Field(ge=1, le=10000)
    target_wait_minutes: Nonnegative = 30


class QueueRequest(Contract):
    provenance: Provenance
    pools: list[QueuePool] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique(self):
        if len({p.pool_id for p in self.pools}) != len(self.pools):
            raise ValueError("pool_id 必须唯一，共享人员不能重复计入独立池")
        return self


class QueueMetrics(Contract):
    pool_id: str
    stable: bool
    offered_load_fte: float
    utilization: float
    wait_probability: float | None = None
    wait_target_probability: float | None = None
    mean_wait_minutes: float | None = None
    mean_cycle_minutes: float | None = None
    mean_queue_length: float | None = None
    mean_in_system: float | None = None
    diagnostic: str | None = None


class QueueAnalysis(Contract):
    provenance: Provenance
    pools: list[QueueMetrics]
    assumptions: list[str]


class WorkCalendar(Contract):
    """所有资源使用同一本地工作日历；start_date 的 00:00 为仿真时间原点。"""

    start_date: date
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4], min_length=1, max_length=7)
    start_hour: int = Field(default=9, ge=0, le=23)
    end_hour: int = Field(default=17, ge=1, le=24)
    holidays: list[date] = Field(default_factory=list, max_length=365)

    @model_validator(mode="after")
    def valid(self):
        if self.end_hour <= self.start_hour:
            raise ValueError("工作结束时间必须晚于开始时间")
        if len(set(self.weekdays)) != len(self.weekdays) or any(d not in range(7) for d in self.weekdays):
            raise ValueError("weekdays 必须为不重复的 0..6")
        return self


class Reviewer(Contract):
    employee_id: Name
    hourly_cost: Nonnegative = 0
    speed: Positive = 1


class NodeTiming(Contract):
    node_id: Name
    mean_service_minutes: Positive
    distribution: Literal["fixed", "exponential"] = "fixed"
    reject_probability: Probability = 0


class ScriptedAction(Contract):
    node_id: Name
    visit: int = Field(ge=1, le=100)
    action: Literal["agree", "reject", "withdraw", "transfer"]
    transfer_to: Name | None = None

    @model_validator(mode="after")
    def target(self):
        if (self.action == "transfer") != (self.transfer_to is not None):
            raise ValueError("只有转交操作必须提供 transfer_to")
        return self


class SimulationCase(Contract):
    case_id: Name
    arrival_minutes: Nonnegative
    approval: ResolvedApproval
    timings: list[NodeTiming] = Field(max_length=9)
    scripted_actions: list[ScriptedAction] = Field(default_factory=list, max_length=100)
    resubmit_delay_minutes: Nonnegative = 60

    @model_validator(mode="after")
    def unique(self):
        if len({t.node_id for t in self.timings}) != len(self.timings):
            raise ValueError("节点耗时配置不能重复")
        keys = [(a.node_id, a.visit) for a in self.scripted_actions]
        if len(set(keys)) != len(keys):
            raise ValueError("同一节点同一次访问不能配置多个操作")
        return self


class SimulationScenario(Contract):
    name: Name
    provenance: Provenance
    calendar: WorkCalendar
    horizon_minutes: Positive = Field(default=10080, le=527040)
    sla_minutes: Positive = 1440
    seed: int = Field(default=0, ge=0, le=2**32-1)
    max_rework: int = Field(default=3, ge=0, le=10)
    reviewers: list[Reviewer] = Field(min_length=1, max_length=100)
    cases: list[SimulationCase] = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def unique(self):
        if len({r.employee_id for r in self.reviewers}) != len(self.reviewers):
            raise ValueError("同一人员只能出现一次，以免跨节点重复计算能力")
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("case_id 必须唯一")
        if any(c.arrival_minutes >= self.horizon_minutes for c in self.cases):
            raise ValueError("所有申请必须在观察窗口结束前到达")
        return self


class StepTrace(Contract):
    node_id: str
    employee_id: str | None = None
    visit: int
    ready_minutes: float
    start_minutes: float
    end_minutes: float
    service_minutes: float
    action: str


class CaseMetrics(Contract):
    case_id: str
    status: Literal["completed", "incomplete", "rework_limit"]
    arrival_minutes: float
    completed_minutes: float | None
    cycle_minutes: float | None
    rework_count: int
    traces: list[StepTrace]


class NodeMetrics(Contract):
    node_id: str
    handled_visits: int
    mean_wait_minutes: float
    total_service_minutes: float


class ReviewerMetrics(Contract):
    employee_id: str
    busy_minutes: float
    available_minutes: float
    utilization: float


class SimulationSummary(Contract):
    total: int
    completed: int
    incomplete: int
    rework_limit: int
    completion_rate: float
    sla_attainment_rate: float
    mean_cycle_minutes: float | None
    p95_cycle_minutes: float | None
    staffing_cost: float


class SimulationResult(Contract):
    name: str
    provenance: Provenance
    scenario_hash: str
    comparison_key: str
    seed: int
    summary: SimulationSummary
    cases: list[CaseMetrics]
    nodes: list[NodeMetrics]
    reviewers: list[ReviewerMetrics]
    assumptions: list[str]


class OptimizationRequest(Contract):
    scenario: SimulationScenario
    candidate_ids: list[Name] = Field(min_length=1, max_length=10)
    budget: Nonnegative
    minimum_completion_rate: Probability = 1
    minimum_sla_attainment: Probability = 0.95
    solver: Literal["differential_evolution", "enumeration"] = "differential_evolution"
    max_evaluations: int = Field(default=64, ge=2, le=128)
    validation_seed: int = Field(default=314159, ge=0, le=2**32-1)

    @model_validator(mode="after")
    def ids(self):
        ids = {r.employee_id for r in self.scenario.reviewers}
        if len(set(self.candidate_ids)) != len(self.candidate_ids) or not set(self.candidate_ids) <= ids:
            raise ValueError("candidate_ids 必须是 scenario.reviewers 中不重复的人员")
        if self.validation_seed == self.scenario.seed:
            raise ValueError("验证种子必须与搜索种子不同")
        return self


class OptimizationResult(Contract):
    status: Literal["candidate_found", "no_feasible_candidate", "validation_failed"]
    solver: str
    evaluations: int
    search_exhausted: bool
    selected_ids: list[str]
    training: SimulationResult | None = None
    validation: SimulationResult | None = None
    violations: list[str]
    limitations: list[str]


class MetricChange(Contract):
    metric: str
    baseline: float | None
    candidate: float | None
    delta: float | None
    relative_change: float | None
    unit: str


class ScenarioComparison(Contract):
    comparable: bool
    changes: list[MetricChange]
    diagnostics: list[str]
    attribution: dict[str, float] | None = None


class FactorialResults(Contract):
    """同一指标的四个仿真结果：00 基线、10 只改 A、01 只改 B、11 同时改。"""

    only_a: SimulationResult
    only_b: SimulationResult
    metric: Literal["mean_cycle_minutes", "p95_cycle_minutes", "staffing_cost", "sla_attainment_rate"]
