"""FunctionToolset 注册的七个同步工具；重计算由 SDK/计划执行器放入工作线程。"""

from .analytics import analyze_queues, compare_scenarios
from .models import (
    ApproverSnapshot, FactorialResults, LeaveFacts, OptimizationRequest, OptimizationResult,
    QueueAnalysis, QueueRequest, ResolvedApproval, RouteResult, ScenarioComparison,
    SimulationResult, SimulationScenario, WorkflowDefinition,
)
from .optimization import optimize_capacity
from .rules import calculate_route, resolve_approvers, workflow_definition
from .simulation import simulate


def get_leave_workflow() -> WorkflowDefinition:
    """读取内置请假审批流程快照，返回节点、分支条件、审批角色、退回目标和来源版本。

    包含 9 个审批节点和 10 个条件网关。只读本地打包快照，不访问平台或执行通知。
    """
    return workflow_definition()


def calculate_leave_route(facts: LeaveFacts) -> RouteResult:
    """根据已核算的请假事实确定审批路径、命中理由和通知分支；缺失事实返回 needs_data。

    Args:
        facts: 标注来源的申请事实。年度累计须包含本单、有效历史和待汇总数据；证明病假、
            外派部门规则和 HRBP 抄送标记须由业务数据核算提供。工具不自动查询数据库。
    """
    return calculate_route(facts)


def resolve_leave_approvers(route: RouteResult, snapshot: ApproverSnapshot) -> ResolvedApproval:
    """从已解析的组织角色快照匹配审批人，应用有效授权、去重并排除申请人自己。

    Args:
        route: calculate_leave_route 返回的完整路径，工具会重新核对规则和版本。
        snapshot: 平台组织规则已解析的英文角色/部门候选集及有效授权快照；不自行猜测上下级。
    """
    return resolve_approvers(route, snapshot)


def analyze_approval_queues(request: QueueRequest) -> QueueAnalysis:
    """用 Erlang-C 和 Little 定律分析独立审批人员池的负荷、等待达标率和平均积压。

    Args:
        request: 到达率为单/小时、实际处理及等待时间为分钟。人员池须独立；稳态不存在时
            返回 stable=false，不生成有限稳态等待时长。复杂共享资源请使用流程仿真。
    """
    return analyze_queues(request)


def simulate_leave_workflow(scenario: SimulationScenario) -> SimulationResult:
    """仿真请假审批的共享人员、工作日历、或签领取、自动跳过、转交和退回重提，输出时效与成本。

    Args:
        scenario: 显式指定来源、申请路径与人员解析结果、各节点实际处理时长、人员、工作日历、
            到达时间、观察窗口和随机种子。支持固定/指数服务；所有时间均为分钟。
            不运行真实审批，不发送消息；退回后沿相同申请路径重提。
    """
    return simulate(scenario)


def optimize_approval_capacity(request: OptimizationRequest) -> OptimizationResult:
    """在预算和时效目标下选择合格候选人员子集，用整数差分进化或有界枚举搜索并用独立种子复核。

    Args:
        request: 基线场景、可启停候选人员 ID、预算、完成率/SLA 目标和评估预算。
            其余人员保持启用；不改变审批资格或路径。DE 需安装 approval 可选依赖。
    """
    return optimize_capacity(request)


def compare_approval_scenarios(
    baseline: SimulationResult, candidate: SimulationResult,
    factorial: FactorialResults | None = None,
) -> ScenarioComparison:
    """比较同一申请群体和观察口径的仿真指标，返回时长、SLA、完成率和成本差异及可选两因素归因。

    Args:
        baseline: 基线仿真完整结果。
        candidate: 调整后的仿真完整结果；比较条件不一致时返回 comparable=false。
        factorial: 可选的只改 A、只改 B 两组结果及指标，连同 baseline/candidate 构成 2×2 归因。
    """
    return compare_scenarios(baseline, candidate, factorial)


APPROVAL_TOOLS = (
    get_leave_workflow, calculate_leave_route, resolve_leave_approvers,
    analyze_approval_queues, simulate_leave_workflow, optimize_approval_capacity,
    compare_approval_scenarios,
)
