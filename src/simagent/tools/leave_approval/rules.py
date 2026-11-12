"""源流程的只读快照及规则适配；数据库核算和组织解析通过显式快照提供。"""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

from .models import (
    ApprovalStep, ApproverSnapshot, LeaveFacts, ResolvedApproval, ResolvedStep,
    RouteResult, WorkflowDefinition,
)


@lru_cache(maxsize=1)
def _workflow() -> WorkflowDefinition:
    path = files(__package__).joinpath("assets/workflow.json")
    flow = WorkflowDefinition.model_validate_json(path.read_text(encoding="utf-8"))
    ids = {n.id for n in flow.nodes}
    if len(ids) != len(flow.nodes):
        raise ValueError("流程快照有重复节点")
    for node in flow.nodes:
        if not set(node.next_ids + node.reject_to + node.withdraw_to) <= ids:
            raise ValueError("流程快照含未知目标节点")
    return flow


def workflow_definition() -> WorkflowDefinition:
    return _workflow().model_copy(deep=True)


def calculate_route(facts: LeaveFacts) -> RouteResult:
    flow = _workflow()
    result = RouteResult(
        status="needs_data", provenance=facts.provenance,
        application_id=facts.application_id, applicant_id=facts.applicant_id,
        department_code=facts.department_code, workflow_version=flow.version,
        workflow_sha256=flow.source_sha256, facts=facts,
        limitations=[
            "年度累计、证明病假分类和 HRBP 抄送标记由输入快照提供；本工具不查询平台数据库。",
            "年度累计须按源规则包含本单、有效历史与待汇总记录，并处理销改、工作日历和跨年最大值。",
            "路径计算不表示申请已经获批；保存和通知节点仅作流程记录。",
        ],
    )
    payroll = facts.payroll.strip().upper()
    if payroll not in {"CNCJ", "CNKL"}:
        result.status = "not_applicable"
        return result
    types = set(facts.leave_types)
    sick = "SICK_LEAVE" in types
    personal = "PERSONAL_LEAVE" in types
    required = ["notify_hrbp"]
    if payroll == "CNCJ":
        required.append("subsidiary_rule_matched")
    if personal:
        required.append("annual_personal_days")
    if sick:
        required.extend(["annual_sick_days", "sick_certificate"])
        if facts.sick_certificate == "certified":
            required.extend(["medical_period_exceeded", "health_check_required"])
    result.missing_fields = [name for name in required if getattr(facts, name) is None]
    if result.missing_fields:
        return result
    # 与源 flowCalcYearDays 保持相同口径：本单未出现的假别不参与该假别累计判断。
    sick_days = facts.annual_sick_days if sick else 0
    personal_days = facts.annual_personal_days if personal else 0
    certified = sick and facts.sick_certificate == "certified"
    over_medical = certified and facts.medical_period_exceeded is True
    review_types = {
        "PRENATAL_EXAMINATION_LEAVE", "FAMILY_PLANNING_LEAVE", "PREMARITAL_EXAMINATION_LEAVE",
        "MARRIAGE_LEAVE", "PATERNITY_LEAVE", "ABORTION_LEAVE", "WORK_INJURY_LEAVE",
    }
    enter_hr = personal_days > 12 or sick_days > 22 or over_medical
    notify_hr = not enter_hr and not over_medical and (
        personal or (sick and 10 < sick_days <= 22 and personal_days <= 12)
    )
    flags = {
        "enterKaoqin": "MATERNITY_LEAVE" in types or certified or bool(types & review_types),
        "enterHealth": certified and (over_medical or facts.health_check_required is True),
        "enterDirectManager": True,
        "enterSubsidiaryProjectLeader": payroll == "CNCJ" and facts.subsidiary_rule_matched is True,
        "enterUpperManager": sick_days > 10 or personal_days > 12 or over_medical,
        "enterSecurityTeam": over_medical,
        "enterHr": enter_hr or notify_hr,
        "notifyHr": notify_hr,
        "notifyHrbp": facts.notify_hrbp is True,
        "enterDeptPresident": personal_days > 22 or over_medical,
        "enterChr": personal_days > 66 or over_medical,
    }
    reasons = {
        "ATTENDANCE_CLERK": "本单含产假、证明病假或需考勤复核的假别",
        "HEALTH_GUIDANCE_CENTER": "证明病假且超医疗期或要求健康核检",
        "DIRECT_MANAGER": "适用假别的直接主管必经节点",
        "SUBSIDIARY_OWNER": "CNCJ 且输入快照确认外派部门规则命中",
        "UPPER_ORG_MANAGER": "年度病假 > 10 天、年度事假 > 12 天或证明病假超医疗期",
        "EMPLOYEE_SUPPORT_TEAM": "证明病假且超医疗期",
        "L1_HR_DEPT_HEAD": "notifyHr 命中，自动审批后通知" if notify_hr else "年度事假 > 12 天、病假 > 22 天或证明病假超医疗期",
        "L1_ORG_PRESIDENT": "年度事假 > 22 天或证明病假超医疗期",
        "COMPANY_HR_PRESIDENT": "年度事假 > 66 天或证明病假超医疗期",
    }
    nodes = {n.id: n for n in flow.nodes}
    current = next(n.id for n in flow.nodes if n.kind == "start")
    while True:
        if current in result.visited_nodes:
            raise ValueError("正常前向路径出现循环")
        result.visited_nodes.append(current)
        node = nodes[current]
        if node.kind == "approval":
            automatic = bool(node.auto_condition and flags[node.auto_condition.flag] == node.auto_condition.equals)
            result.steps.append(ApprovalStep(
                node_id=node.id, name=node.name, role_code=node.role_code,
                auto_approve=automatic, skip_same_handler=node.skip_same_handler,
                reason=reasons[node.role_code],
            ))
        if node.kind == "email":
            result.notifications.append(node.name)
        if node.kind == "end":
            break
        targets = [b.target_id for b in node.branches if flags[b.flag] == b.equals] if node.branches else node.next_ids
        if len(targets) != 1:
            raise ValueError(f"节点 {node.name} 未命中唯一后继")
        current = targets[0]
    result.flags, result.status = flags, "ready"
    return result


def verify_route(route: RouteResult) -> None:
    if calculate_route(route.facts) != route:
        raise ValueError("审批路径与事实或当前流程版本不一致，请重新计算路径")


def resolve_approvers(route: RouteResult, snapshot: ApproverSnapshot) -> ResolvedApproval:
    verify_route(route)
    result = ResolvedApproval(status=route.status, route=route, snapshot=snapshot)
    if route.status != "ready":
        result.diagnostics.append("路径未就绪，不能确定审批人")
        return result
    bindings = {(b.role_code, b.department_code): b.employee_ids for b in snapshot.bindings}
    for step in route.steps:
        candidates = bindings.get((step.role_code, route.department_code), [])
        eligible = []
        for employee in candidates:
            matches = [d for d in snapshot.delegations if
                d.authorizer_id == employee and d.department_code == route.department_code
                and d.applicant_id in {None, route.applicant_id}
                and d.valid_from <= snapshot.as_of <= d.valid_until]
            # 快照中的申请人专属授权优先；重叠的同优先级授权应由上游解决。
            specific = [d for d in matches if d.applicant_id == route.applicant_id]
            matches = specific or matches
            if len(matches) > 1:
                raise ValueError(f"审批人 {employee} 的授权快照存在歧义")
            replacements = matches[0].delegate_ids if matches else [employee]
            for person in replacements:
                if person != route.applicant_id and person not in eligible:
                    eligible.append(person)
        if not eligible and not step.auto_approve:
            result.missing_roles.append(step.role_code)
        result.steps.append(ResolvedStep(**step.model_dump(), eligible_ids=eligible))
    if result.missing_roles:
        result.status = "needs_data"
        result.diagnostics.append("人工审批节点缺少有效审批人，不能按自动通过处理")
    result.diagnostics.append("候选集须由平台组织层级规则预先解析；英文角色编码与平台名称的映射由快照提供方负责。")
    result.diagnostics.append("共享人员保留同一 employee_id；不将同一个人在不同节点重复计为独立资源。")
    return result


def verify_approval(approval: ResolvedApproval) -> None:
    if resolve_approvers(approval.route, approval.snapshot) != approval:
        raise ValueError("审批人结果与快照不一致，请重新解析")
    if approval.status != "ready":
        raise ValueError("审批路径或审批人资料未就绪")
