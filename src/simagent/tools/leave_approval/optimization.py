"""从给定合格人员中选择启用子集，使用整数 DE 或有界枚举，再独立种子复核。"""

from __future__ import annotations

import itertools

from .models import OptimizationRequest, OptimizationResult, SimulationScenario
from .simulation import simulate, validate_scenario


class _BudgetReached(Exception):
    pass


def optimize_capacity(request: OptimizationRequest) -> OptimizationResult:
    validate_scenario(request.scenario)
    if request.max_evaluations * len(request.scenario.cases) * (request.scenario.max_rework + 1) > 100000:
        raise ValueError("优化预算与场景规模乘积过大，请减少评估次数、申请数或重提次数")
    de = None
    if request.solver == "differential_evolution":
        try:
            from scipy.optimize import differential_evolution
        except ImportError:
            raise ValueError("差分进化需要 approval 可选依赖：uv sync --extra web --extra approval；也可显式选择 enumeration") from None
        de = differential_evolution
    candidates = set(request.candidate_ids)
    fixed = [r.employee_id for r in request.scenario.reviewers if r.employee_id not in candidates]
    cache = {}
    best = None
    best_ids = []
    last_violations = []

    def violations(run):
        problems = []
        if run.summary.staffing_cost > request.budget + 1e-9:
            problems.append("人员成本超过预算")
        if run.summary.completion_rate + 1e-12 < request.minimum_completion_rate:
            problems.append("完成率未达到目标")
        if run.summary.sla_attainment_rate + 1e-12 < request.minimum_sla_attainment:
            problems.append("SLA 达标率未达到目标")
        return problems

    def evaluate(vector):
        nonlocal best, best_ids, last_violations
        key = tuple(int(round(float(x))) for x in vector)
        if key in cache:
            return cache[key]
        if len(cache) >= request.max_evaluations:
            raise _BudgetReached
        ids = fixed + [person for person, enabled in zip(request.candidate_ids, key) if enabled]
        data = request.scenario.model_dump(mode="python")
        data["reviewers"] = [r for r in request.scenario.reviewers if r.employee_id in ids]
        try:
            scenario = SimulationScenario.model_validate(data)
            run = simulate(scenario)
        except ValueError as error:
            last_violations = [str(error)]
            cache[key] = 1000000.0
            return cache[key]
        problems = violations(run)
        if not problems:
            if best is None or run.summary.staffing_cost < best.summary.staffing_cost:
                best, best_ids = run, ids
            score = run.summary.staffing_cost / max(1.0, request.budget)
        else:
            last_violations = problems
            score = 2.0 + max(0, run.summary.staffing_cost - request.budget) / max(1.0, request.budget)
            score += max(0, request.minimum_completion_rate - run.summary.completion_rate)
            score += max(0, request.minimum_sla_attainment - run.summary.sla_attainment_rate)
        cache[key] = score
        return score

    search_exhausted = False
    try:
        # 首先评估所有给定人员均启用的基线，避免短预算遗漏已知可行配置。
        evaluate([1] * len(candidates))
        if request.solver == "enumeration":
            for mask in itertools.product([0, 1], repeat=len(candidates)):
                evaluate(mask)
            search_exhausted = True
        else:
            # 与 operation/algorithms/de.py 相同的 SciPy 后端；此处补齐业务目标、整数域和预算。
            de(evaluate, bounds=[(0, 1)] * len(candidates), integrality=[True] * len(candidates),
               seed=request.scenario.seed, maxiter=max(1, request.max_evaluations // 5),
               popsize=5, polish=False, workers=1, updating="immediate")
            search_exhausted = len(cache) == 2 ** len(candidates)
    except _BudgetReached:
        pass
    limitations = [
        "只选择快照中已合格的候选人员；固定人员保持启用，不修改法定路径、审批资格或业务阈值。",
        "成本按整个观察窗口的可用工时计算；结果依赖输入的成本、耗时和业务样本。",
        "差分进化为启发式候选搜索；枚举只有穷尽给定人员子集时才覆盖该有限搜索空间。",
        "验证使用一个不同随机种子，属于复核而非统计置信保证；上线前应补充多种子和真实数据验证。",
    ]
    if best is None:
        return OptimizationResult(status="no_feasible_candidate", solver=request.solver,
                                  evaluations=len(cache), search_exhausted=search_exhausted,
                                  selected_ids=[], violations=last_violations, limitations=limitations)
    data = request.scenario.model_dump(mode="python")
    data["reviewers"] = [r for r in request.scenario.reviewers if r.employee_id in best_ids]
    data["seed"] = request.validation_seed
    validation = simulate(SimulationScenario.model_validate(data))
    problems = violations(validation)
    return OptimizationResult(
        status="validation_failed" if problems else "candidate_found", solver=request.solver,
        evaluations=len(cache), search_exhausted=search_exhausted, selected_ids=best_ids,
        training=best, validation=validation, violations=problems, limitations=limitations,
    )
