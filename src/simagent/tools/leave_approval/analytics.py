"""稳定 Erlang-C 递推、Little 定律及可比情景指标差异。"""

from __future__ import annotations

import math

from .models import (
    FactorialResults, MetricChange, QueueAnalysis, QueueMetrics, QueueRequest,
    ScenarioComparison, SimulationResult,
)


def analyze_queues(request: QueueRequest) -> QueueAnalysis:
    metrics = []
    for pool in request.pools:
        a = pool.arrival_per_hour * (pool.mean_service_minutes / 60)
        if not math.isfinite(a):
            raise ValueError("到达率与服务时长的乘积超出可计算范围")
        rho = a / pool.servers
        row = QueueMetrics(pool_id=pool.pool_id, stable=rho < 1, offered_load_fte=a, utilization=rho)
        if rho >= 1:
            row.diagnostic = "到达率不低于总服务率，稳态不存在；不能报告有限的稳态等待时间。"
        else:
            # Erlang-B 递推再转换 Erlang-C，避免原始阶乘/幂递推在大容量时溢出。
            b = 1.0
            for i in range(1, pool.servers + 1):
                b = a * b / (i + a * b)
            pw = b / (1 - rho + rho * b)
            spare = (pool.servers - a) / pool.mean_service_minutes
            wait = pw / spare
            row.wait_probability = pw
            row.wait_target_probability = 1 - pw if pool.target_wait_minutes == 0 else 1 - pw * math.exp(-spare * pool.target_wait_minutes)
            row.mean_wait_minutes = wait
            row.mean_cycle_minutes = wait + pool.mean_service_minutes
            row.mean_queue_length = pool.arrival_per_hour / 60 * wait
            row.mean_in_system = pool.arrival_per_hour / 60 * row.mean_cycle_minutes
        # 赋值后的派生数值也必须经过校验，防止极端有限输入产生溢出结果。
        metrics.append(QueueMetrics.model_validate(row.model_dump()))
    return QueueAnalysis(provenance=request.provenance, pools=metrics, assumptions=[
        "每个池独立，近似泊松到达、指数服务、同速率并行人员和 FCFS；不考虑撤回或跨池共享人员。",
        "服务时长为实际处理分钟，排队 SLA 指等待时间而非流程总时间；跨工作日使用仿真工具。",
        "offered_load_fte 是平均负荷，不是满足 SLA 所需的最少人数。",
    ])


def compare_scenarios(
    baseline: SimulationResult, candidate: SimulationResult,
    factorial: FactorialResults | None = None,
) -> ScenarioComparison:
    comparable = baseline.comparison_key == candidate.comparison_key
    result = ScenarioComparison(comparable=comparable, changes=[], diagnostics=[])
    if not comparable:
        result.diagnostics.append("申请群体、到达时间、日历、观察窗口、SLA 或来源版本不同，拒绝直接归因比较。")
        return result
    if baseline.seed != candidate.seed:
        result.diagnostics.append("随机种子不同，指标差异同时包含抽样波动。")
    if baseline.provenance.source == "demo" or candidate.provenance.source == "demo":
        result.diagnostics.append("比较包含演示数据，不能视为真实业务改善效果。")
    units = {"mean_cycle_minutes": "min", "p95_cycle_minutes": "min", "staffing_cost": "cost_unit", "completion_rate": "ratio", "sla_attainment_rate": "ratio"}
    for key, unit in units.items():
        a, b = getattr(baseline.summary, key), getattr(candidate.summary, key)
        delta = None if a is None or b is None else b - a
        relative = None if delta is None or a == 0 else delta / a
        result.changes.append(MetricChange(metric=key, baseline=a, candidate=b, delta=delta, relative_change=relative, unit=unit))
    if baseline.summary.incomplete or candidate.summary.incomplete or baseline.summary.rework_limit or candidate.summary.rework_limit:
        result.diagnostics.append("时长均值和 P95 仅统计已完成申请，须结合完成率判断，不能忽略未完成与重提上限。")
    if factorial:
        runs = [baseline, factorial.only_a, factorial.only_b, candidate]
        if any(r.comparison_key != baseline.comparison_key or r.seed != baseline.seed for r in runs):
            raise ValueError("两因素归因必须使用同一比较条件和随机种子")
        values = [getattr(r.summary, factorial.metric) for r in runs]
        if any(v is None for v in values):
            raise ValueError("归因指标缺失，不能把未完成时长当作 0")
        y00, y10, y01, y11 = values
        result.attribution = {
            "factor_a_effect": 0.5 * ((y10 - y00) + (y11 - y01)),
            "factor_b_effect": 0.5 * ((y01 - y00) + (y11 - y10)),
            "interaction": y11 - y10 - y01 + y00,
        }
        result.diagnostics.append("采用现有 counterfactual_split 的两因素 Shapley 公式；交互项已分摊进 A/B，不能再与两者相加。")
    return result
