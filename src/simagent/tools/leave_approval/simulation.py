"""有共享人员、工作日历和退回重提的有界离散事件仿真；不执行外部审批。"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import json
import math
import random
import statistics
from collections import defaultdict
from datetime import timedelta

from .models import (
    CaseMetrics, NodeMetrics, ReviewerMetrics, SimulationResult,
    SimulationScenario, SimulationSummary, StepTrace, WorkCalendar,
)
from .rules import verify_approval


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


class Calendar:
    def __init__(self, spec: WorkCalendar):
        self.spec = spec
        self.holidays = set(spec.holidays)

    def window(self, day: int):
        date = self.spec.start_date + timedelta(days=day)
        if date.weekday() not in self.spec.weekdays or date in self.holidays:
            return None
        return day * 1440 + self.spec.start_hour * 60, day * 1440 + self.spec.end_hour * 60

    def next_open(self, time: float) -> float:
        day = math.floor(time / 1440)
        for offset in range(740):
            window = self.window(day + offset)
            if window and time < window[1]:
                return max(time, window[0])
        raise ValueError("工作日历在可计算窗口内没有可用时间")

    def finish(self, time: float, minutes: float) -> float:
        time = self.next_open(time)
        for _ in range(740):
            end = self.window(math.floor(time / 1440))[1]
            available = end - time
            if minutes <= available:
                return time + minutes
            minutes -= available
            time = self.next_open(end)
        raise ValueError("单项服务耗时超出 740 个工作日计算上限")

    def between(self, start: float, end: float) -> float:
        if end <= start:
            return 0.0
        total = 0.0
        for day in range(math.floor(start / 1440), math.floor(end / 1440) + 1):
            window = self.window(day)
            if window:
                total += max(0, min(end, window[1]) - max(start, window[0]))
        return total


def validate_scenario(scenario: SimulationScenario) -> None:
    active = {r.employee_id for r in scenario.reviewers}
    for case in scenario.cases:
        verify_approval(case.approval)
        if case.approval.snapshot.as_of > scenario.calendar.start_date + timedelta(days=int(case.arrival_minutes // 1440)):
            raise ValueError("审批人快照日期不能晚于申请到达日期")
        steps = {s.node_id: s for s in case.approval.steps}
        if case.approval.route.application_id != case.case_id:
            raise ValueError("case_id 必须与路径中的 application_id 一致")
        timing = {t.node_id: t for t in case.timings}
        if set(timing) - steps.keys():
            raise ValueError("耗时配置包含未经过的审批节点")
        for step in steps.values():
            if not step.auto_approve and (step.node_id not in timing or not active.intersection(step.eligible_ids)):
                raise ValueError(f"节点 {step.name} 缺少处理时长或已启用的合格人员")
        for action in case.scripted_actions:
            step = steps.get(action.node_id)
            if step is None or step.auto_approve:
                raise ValueError("脚本操作必须指向人工审批节点")
            if action.transfer_to and (action.transfer_to not in step.eligible_ids or action.transfer_to not in active):
                raise ValueError("转交目标必须是该节点已启用的合格审批人")


def simulate(scenario: SimulationScenario) -> SimulationResult:
    validate_scenario(scenario)
    calendar = Calendar(scenario.calendar)
    reviewers = {r.employee_id: r for r in scenario.reviewers}
    busy = {person: False for person in reviewers}
    intervals = defaultdict(list)
    serial = itertools.count()
    events = []
    pending = []
    wake_times = set()
    states = [{"stage": 0, "visits": defaultdict(int), "last": None, "rework": 0,
               "status": "incomplete", "completed": None, "traces": []} for _ in scenario.cases]

    def push(time, kind, index, payload=None):
        heapq.heappush(events, (time, next(serial), kind, index, payload))

    def ready(index, time, forced=None):
        case, state = scenario.cases[index], states[index]
        steps = case.approval.steps
        while state["stage"] < len(steps):
            step = steps[state["stage"]]
            same_person = (forced is None and step.skip_same_handler and len(step.eligible_ids) == 1
                           and step.eligible_ids[0] == state["last"])
            if same_person and any(a.node_id == step.node_id and a.visit == state["visits"][step.node_id] + 1
                                   for a in case.scripted_actions):
                raise ValueError("同人跳过的节点不能同时指定人工操作脚本")
            if not step.auto_approve and not same_person:
                pending.append((index, time, forced))
                return
            state["visits"][step.node_id] += 1
            state["traces"].append(StepTrace(
                node_id=step.node_id, employee_id=state["last"] if same_person else None,
                visit=state["visits"][step.node_id], ready_minutes=time, start_minutes=time,
                end_minutes=time, service_minutes=0, action="same_handler_skip" if same_person else "auto_approve",
            ))
            state["stage"] += 1
        state["status"], state["completed"] = "completed", time

    def draw(index, node_id, visit, purpose):
        key = [scenario.seed, scenario.cases[index].case_id, node_id, visit, purpose]
        return random.Random(int(digest(key), 16))

    for index, case in enumerate(scenario.cases):
        push(case.arrival_minutes, "ready", index)
    processed = 0
    while events:
        now = events[0][0]
        if now > scenario.horizon_minutes:
            break
        while events and events[0][0] == now:
            _, _, kind, index, payload = heapq.heappop(events)
            processed += 1
            if processed > 100000:
                raise ValueError("仿真事件超过 100000 上限，请缩小场景")
            if kind == "wake":
                wake_times.discard(now)
                continue
            case, state = scenario.cases[index], states[index]
            if kind == "ready":
                ready(index, now, payload)
                continue
            trace, action, transfer_to = payload
            busy[trace.employee_id] = False
            trace.action = action
            if action in {"reject", "withdraw"}:
                state["rework"] += 1
                state["last"] = None
                if state["rework"] > scenario.max_rework:
                    state["status"] = "rework_limit"
                else:
                    state["stage"] = 0
                    push(now + case.resubmit_delay_minutes, "ready", index)
            elif action == "transfer":
                push(now, "ready", index, transfer_to)
            else:
                state["last"] = trace.employee_id
                state["stage"] += 1
                ready(index, now)
        if not pending:
            continue
        next_open = calendar.next_open(now)
        if next_open > now:
            if next_open not in wake_times:
                push(next_open, "wake", -1)
                wake_times.add(next_open)
            continue
        remaining = []
        # 每个或签任务由一个空闲候选领取；多个角色共享同一 reviewer 的占用状态。
        for index, arrived, forced in sorted(pending, key=lambda p: (p[1], p[0])):
            case, state = scenario.cases[index], states[index]
            step = case.approval.steps[state["stage"]]
            eligible = [forced] if forced else sorted(step.eligible_ids)
            person = next((p for p in eligible if p in busy and not busy[p]), None)
            if person is None:
                remaining.append((index, arrived, forced))
                continue
            state["visits"][step.node_id] += 1
            visit = state["visits"][step.node_id]
            if visit > 100:
                raise ValueError("同一节点访问次数超过 100，请检查转交脚本和退回配置")
            timing = next(t for t in case.timings if t.node_id == step.node_id)
            duration = timing.mean_service_minutes
            if timing.distribution == "exponential":
                duration = draw(index, step.node_id, visit, "service").expovariate(1 / duration)
            duration /= reviewers[person].speed
            end = calendar.finish(now, duration)
            scripted = next((a for a in case.scripted_actions if a.node_id == step.node_id and a.visit == visit), None)
            action = scripted.action if scripted else (
                "reject" if draw(index, step.node_id, visit, "action").random() < timing.reject_probability else "agree"
            )
            trace = StepTrace(node_id=step.node_id, employee_id=person, visit=visit,
                              ready_minutes=arrived, start_minutes=now, end_minutes=end,
                              service_minutes=duration, action="in_progress")
            state["traces"].append(trace)
            intervals[person].append((now, end))
            busy[person] = True
            push(end, "done", index, (trace, action, scripted.transfer_to if scripted else None))
        pending = remaining

    cases = []
    node_traces = defaultdict(list)
    for case, state in zip(scenario.cases, states):
        for trace in state["traces"]:
            if trace.action == "in_progress":
                trace.end_minutes = scenario.horizon_minutes
                trace.service_minutes = calendar.between(trace.start_minutes, trace.end_minutes)
            if trace.action not in {"auto_approve", "same_handler_skip"}:
                node_traces[trace.node_id].append(trace)
        completion = state["completed"]
        cases.append(CaseMetrics(
            case_id=case.case_id, status=state["status"], arrival_minutes=case.arrival_minutes,
            completed_minutes=completion, cycle_minutes=None if completion is None else completion - case.arrival_minutes,
            rework_count=state["rework"], traces=state["traces"],
        ))
    completed = [c for c in cases if c.status == "completed"]
    cycles = sorted(c.cycle_minutes for c in completed)
    p95 = None
    if cycles:
        pos = (len(cycles) - 1) * 0.95
        low, high = math.floor(pos), math.ceil(pos)
        p95 = cycles[low] + (cycles[high] - cycles[low]) * (pos - low)
    available = calendar.between(0, scenario.horizon_minutes)
    resource_metrics = []
    for person in reviewers:
        occupied = sum(calendar.between(start, min(end, scenario.horizon_minutes)) for start, end in intervals[person])
        resource_metrics.append(ReviewerMetrics(
            employee_id=person, busy_minutes=occupied, available_minutes=available,
            utilization=occupied / available if available else 0,
        ))
    costs = sum(r.hourly_cost * available / 60 for r in reviewers.values())
    document = scenario.model_dump(mode="json")
    comparison = {k: document[k] for k in ["calendar", "horizon_minutes", "sla_minutes", "max_rework", "provenance"]}
    comparison["cases"] = [{k: c[k] for k in ["case_id", "arrival_minutes", "approval", "scripted_actions", "resubmit_delay_minutes"]} for c in document["cases"]]
    provenance = scenario.provenance
    if any(c.approval.route.provenance.source == "demo" or c.approval.snapshot.provenance.source == "demo" for c in scenario.cases):
        provenance = provenance.model_copy(update={"source": "demo"})
    return SimulationResult(
        name=scenario.name, provenance=provenance, scenario_hash=digest(document), comparison_key=digest(comparison), seed=scenario.seed,
        summary=SimulationSummary(
            total=len(cases), completed=len(completed), incomplete=sum(c.status == "incomplete" for c in cases),
            rework_limit=sum(c.status == "rework_limit" for c in cases), completion_rate=len(completed)/len(cases),
            sla_attainment_rate=sum(c.cycle_minutes <= scenario.sla_minutes for c in completed)/len(cases),
            mean_cycle_minutes=statistics.fmean(cycles) if cycles else None, p95_cycle_minutes=p95,
            staffing_cost=costs,
        ),
        cases=cases,
        nodes=[NodeMetrics(node_id=node_id, handled_visits=sum(t.action != "in_progress" for t in traces),
                           mean_wait_minutes=statistics.fmean(t.start_minutes-t.ready_minutes for t in traces),
                           total_service_minutes=sum(t.service_minutes for t in traces)) for node_id, traces in sorted(node_traces.items())],
        reviewers=resource_metrics,
        assumptions=[
            "只读仿真，不保存真实业务单据、不发送通知、不提交实际审批。",
            "所有人员共享一个本地工作日历；FCFS 分派到首个空闲合格人员，或签采用单人领取，不模拟多人同时处理。",
            "同人跳过仅在唯一候选等于上一实际同意人时生效；显式自动审批耗时为 0。",
            "驳回和撤回均返回提交，重提保持原申请事实与路径；申请变更需要先重新计算路径。",
            "人员资格和授权使用快照冻结；超出授权有效期的长期场景应分段重新解析快照。",
            "均值与 P95 只包含已完成申请；SLA 达标率以全部到达申请为分母，未完成不会被记为达标。",
            "成本按整个观察窗口的可用工作小时计费，包含空闲人员；service_minutes 是实际工作分钟。",
            "随机抽样按 seed/申请/节点/访问次数确定，便于资源方案使用共同随机数比较。",
        ],
    )
