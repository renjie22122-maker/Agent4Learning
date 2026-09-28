"""Lab 03: 从 0 搭建生产级 Agent 平台 —— 核心落地的工程顺序。

对应生产问题
------------
* 「从 0 搭建一个生产级的企业级 agent 平台，核心落地的工程顺序是什么？」

这个 lab **不压测**，做两件事：

1. **推演"顺序错了会怎样"**：用虚拟时钟推演两支团队的推进顺序（先做花哨编排 vs
   先打地基），把代价换算成可比较的数字：上线时机、上线时覆盖的发布门禁数、
   故障暴露时间、返工工时。其中三个"缺了它就会发生"的故障是**真实跑出来的**：
   - 没有 trace：故障归因只能一个阶段一个阶段地猜（真实部署+复现的代价）；
   - 没有 deadline：一次上游挂死就把整条链路拖满（真实计时）；
   - 没有成本账：账单只能按请求数摊，和真实成本对不上（真实 CostLedger 对账）。
2. **给出一张可执行的路线图**：8 层、每层的 **前置依赖 / 缺了它的典型故障 /
   验收门槛（可度量的 DoD）**，以及每层的建议动作清单。

工程结论：平台的落地顺序不是"哪个功能酷"，而是**依赖顺序** —— 观测（L1）在
可靠性（L2）之前，可靠性在成本（L3）之前……每一层都是下一层的验收工具。顺序
错了不是"晚一点做"，而是**要用返工和事故来买**，而且价格写在下面。
"""

from __future__ import annotations

import sys
import time
import tracemalloc  # noqa: F401  (占位保留：本 lab 不用内存观测)
from dataclasses import dataclass, field

from agentlab.clock import VirtualClock
from agentlab.metrics import METRICS
from agentlab.providers import LLMError, LLMServer, system, user
from agentlab.tracing import Tracer
from agentlab.util import (
    BROKEN, FIX, VERIFY, Stats, head, improvement, kv, lab, note, phase, run_concurrently,
    takeaway,
)

LAB_ID = "lab-03-startup-order"

# ---------------------------------------------------------------------------
# 演示 1：没有 trace 时，归因只能靠猜
# ---------------------------------------------------------------------------
#: (阶段, 正常耗时 ms)。LLM 段是本次事故的罪魁祸首（被上游拖慢 4 倍）。
STAGES: list[tuple[str, float]] = [
    ("auth", 4), ("retrieve", 18), ("rerank", 12), ("llm", 55),
    ("tools", 16), ("guard", 5), ("format", 7),
]
CULPRIT = "llm"
SLOW_FACTOR = 4.0
#: 一次"改代码 + 提测 + 发布 + 等就绪 + 复现一轮"的代价。生产上这一轮 ≈ 20 分钟，
#: 本 lab 用 180ms 代替 —— 比例与结论不变，只是把墙钟压缩到可接受范围。
PROBE_DEPLOY_MS = 180.0


def _run_pipeline() -> dict[str, float]:
    """真实跑一遍链路（真实 sleep），返回每段耗时 ms。"""
    out: dict[str, float] = {}
    for name, ms in STAGES:
        t0 = time.perf_counter()
        time.sleep(ms * (SLOW_FACTOR if name == CULPRIT else 1.0) / 1000.0)
        out[name] = (time.perf_counter() - t0) * 1000.0
    return out


def _blind_attribution() -> dict:
    """没有 trace：只能一段一段加打点、重新发布、复现，直到猜中为止。"""
    t0 = time.perf_counter()
    probes = 0
    hit = ""
    for name, base_ms in STAGES:  # 从第一段开始按顺序排查（最坏情况）
        time.sleep(PROBE_DEPLOY_MS / 1000.0)  # 改代码 + 部署 + 等就绪
        seen = _run_pipeline()[name]  # 复现一轮，只看这一段
        probes += 1
        if seen > base_ms * 1.5:
            hit = name
            break
    return {"probes": probes, "hit": hit, "mttr_ms": (time.perf_counter() - t0) * 1000.0}


def _trace_attribution() -> dict:
    """有 trace：跑一轮带 span 的请求，直接读"谁吃掉了耗时"。"""
    t0 = time.perf_counter()
    tr = Tracer("incident-1")
    with tr.span("agent_request"):
        for name, ms in STAGES:
            with tr.span(name):
                time.sleep(ms * (SLOW_FACTOR if name == CULPRIT else 1.0) / 1000.0)
    duration = {name: tr.durations(name)[0] for name, _ in STAGES}
    hit = max(duration, key=lambda k: duration[k])  # 归因 = 读表，不是猜
    time.sleep(0.02)  # 打开 trace 面板的代价
    mttr = (time.perf_counter() - t0) * 1000.0
    return {"hit": hit, "mttr_ms": mttr, "tracer": tr, "duration": duration}


# ---------------------------------------------------------------------------
# 演示 2：没有 deadline，一次上游挂死就拖满整条链路
# ---------------------------------------------------------------------------
def _no_deadline_vs_deadline() -> dict:
    msgs = [system("你是生产级 agent"), user("总结一下这次事故的根因")]
    srv = LLMServer(max_queue=8, max_wait_s=6.0, seed=3)
    srv.hang("mid-32b", True, duration_s=2.0)  # 对端挂死 2s（连上了但永不返回）
    t0 = time.perf_counter()
    try:  # v0：没有预算 → 只能等对端自己好
        srv.call(msgs, model="mid-32b", timeout=None)
    except LLMError as exc:
        note(f"v0 无预算调用返回 {exc.code}")
    waited_v0 = (time.perf_counter() - t0) * 1000.0
    t1 = time.perf_counter()
    try:  # v1：端到端预算 500ms → 到点自救
        srv.call(msgs, model="mid-32b", timeout=0.5)
    except LLMError as exc:
        note(f"v1 带预算调用按预期快速失败: {exc.code}，retryable={exc.retryable}")
    waited_v1 = (time.perf_counter() - t1) * 1000.0
    return {"wait_ms_before": waited_v0, "wait_ms_after": waited_v1}


# ---------------------------------------------------------------------------
# 演示 3：没有成本账，账单无处归因
# ---------------------------------------------------------------------------
def _cost_attribution() -> dict:
    """同一份流量：有 cost ledger（按 tenant 记账）vs 只有一张总账单。

    三个租户跑不同档位的模型 → 请求数占比 ≠ 成本占比，这是"账单无法归因"的本质。
    粗估口径用**错摊率** = Σ|估算-真实| / (2×总账单)，天然落在 0~100%。
    """
    srv = LLMServer(max_queue=64, max_wait_s=6.0, seed=4)
    srv.set_latency("large-400b", 300)
    srv.set_latency("mid-32b", 120)
    srv.set_latency("small-8b", 40)
    plan = [("tenant-a", "large-400b", 12), ("tenant-b", "mid-32b", 12),
            ("tenant-c", "small-8b", 12)]
    msgs = [system("你是生产级 agent"), user("写一段总结")]
    jobs = [(t, m) for t, m, c in plan for _ in range(c)]
    run_concurrently(lambda k: srv.call(msgs, model=jobs[k][1], tenant=jobs[k][0],
                                        tag="incident", timeout=6.0), len(jobs), 12)
    true_by_tenant = dict(srv.ledger.by_tenant)
    total = srv.ledger.usd
    misallocated = 0.0
    for tenant, _model, count in plan:  # 没有成本账时唯一能做的粗估：按请求数摊总账单
        est = total * count / len(jobs)
        misallocated += abs(est - true_by_tenant.get(tenant, 0.0))
    return {"calls": len(jobs), "usd": total, "by_tenant": true_by_tenant,
            "misalloc_pct": misallocated / (2 * total) * 100.0 if total else 0.0}


# ---------------------------------------------------------------------------
# 路线图：8 层，每层带前置依赖 / 缺失故障 / 验收门槛
# ---------------------------------------------------------------------------
@dataclass
class Layer:
    name: str
    hours: float  # 建议投入（人时）
    gates: int  # 这一层交付的发布门禁数
    needs: str  # 前置依赖
    failure: str  # 缺了它的典型故障
    dod: str  # 验收门槛（可度量）
    action: str  # 建议动作清单


LAYERS: list[Layer] = [
    Layer("L1 单请求链路 + 打点(trace/metrics/cost ledger)", 24, 3, "—",
          "故障无法归因，只能靠猜（阶段 1 实测）",
          "每个请求有 trace_id；P95 可按阶段归因；成本可按 tenant/tag 出账",
          "定 trace_id 规范 → 接 Tracer/METRICS/CostLedger → 每个 span 打 tenant/tag → "
          "把「单请求可归因」写进 CI"),
    Layer("L2 可靠性原语(Deadline/Retry/Breaker/Bulkhead)", 20, 2, "L1",
          "一次上游抖动拖死整条链路（阶段 2 实测）",
          "端到端预算 ≤ SLO；重试在预算内；熔断状态可观测",
          "给每个外部调用定预算 → 重试挂同一 deadline → 舱壁限并发 → 熔断阈值按 P99 设"),
    Layer("L3 缓存与成本治理", 18, 2, "L1,L2",
          "账单无处归因、成本随流量线性上涨（阶段 2 实测）",
          "前缀缓存命中率、单位请求成本可对账（误差 < 5%）",
          "稳定前缀提到最前 → 结果缓存加 TTL/容量上限 → 单位成本看板 → 超预算告警"),
    Layer("L4 上下文与状态治理", 20, 1, "L1",
          "上下文爆炸、长会话不可控、成本失控",
          "上下文分级裁剪；会话状态可持久化与恢复",
          "上下文分层(系统/记忆/检索/对话) → 硬性 token 预算裁剪 → 状态外置到存储"),
    Layer("L5 权限与多租户隔离", 22, 2, "L1,L2,L3",
          "越权检索 / 串租户（合规事故，后果最重）",
          "召回阶段即完成权限过滤；租户维度配额与隔离",
          "权限过滤下推到召回 → 租户维度限流/舱壁 → 审计日志 → 越权回归用例"),
    Layer("L6 队列与长任务治理", 24, 2, "L2,L4",
          "长任务丢失、无法断点续跑、重试产生重复副作用",
          "任务可重入 + 幂等；有界队列 + 背压；断点可续",
          "任务状态机 + 幂等键 → 有界队列与背压 → 断点快照 → 死信队列与人工兜底"),
    Layer("L7 评估与发布门禁", 20, 3, "L1..L6",
          "回归不可发现，只能靠用户投诉",
          "黄金集回归达标才可发布；门禁接进 CI",
          "建黄金集(含长尾/对抗样本) → 离线评估脚本 → 门禁接 CI → 灰度+自动回滚"),
    Layer("L8 多智能体 / 高级能力", 28, 0, "L1..L7",
          "把复杂度建在流沙上：一旦出问题无法定位、无法回滚",
          "协作链路有预算、有评估、有回滚开关",
          "子 agent 各自带预算与超时 → 协作链路纳入 trace → 用 L7 的评估验收收益"),
]
TOTAL_GATES = sum(x.gates for x in LAYERS)
CORE_HOURS = sum(x.hours for x in LAYERS[:7])  # L1..L7：地基

#: 错误顺序：先把工时花在"花哨能力"上
WRONG_FEATURES = {"多智能体协作编排": 120.0, "花哨工具/前端": 80.0, "demo 打磨": 60.0}
WRONG_GATES_AT_LAUNCH = 2  # 只有单元测试 + 手工回归
INCIDENTS = 3  # 上线后一个月内暴露出的事故数
INCIDENT_TRIAGE_H = 12.0  # 每次事故的定位 + 止血
RETROFIT_RATIO = 0.35  # 给已建功能补打点/补可靠性：按该功能工时的 35% 计（要动已有代码 + 回归）
PRESSURE_RATIO = 0.25  # 事故压力下补地基比按部就班贵 25%
PLANNED_REFACTOR_H = 12.0  # 正确顺序下仍然要预留的重构


def simulate_order(order: str) -> dict:
    """用虚拟时钟推演两种推进顺序：上线时机、门禁覆盖、返工工时。"""
    clk = VirtualClock()
    weeks = 0.0
    if order == "wrong":
        # ① 先做花哨能力（6.5 周）→ ② 上线 → ③ 事故暴露缺失 → ④ 返工补地基
        feature_h = sum(WRONG_FEATURES.values())
        weeks += feature_h / 40.0
        clk.advance(feature_h / 40.0)
        gates = WRONG_GATES_AT_LAUNCH
        incident_h = INCIDENTS * INCIDENT_TRIAGE_H
        clk.advance(incident_h / 40.0)
        retrofit_h = feature_h * RETROFIT_RATIO + CORE_HOURS * PRESSURE_RATIO
        clk.advance(retrofit_h / 40.0)
        rework = incident_h + CORE_HOURS * PRESSURE_RATIO + feature_h * RETROFIT_RATIO
        total = feature_h + incident_h + CORE_HOURS + feature_h * RETROFIT_RATIO
    else:
        # ① L1..L7 打地基 → ② 上线（门禁齐全）→ ③ 再叠高级能力
        weeks += CORE_HOURS / 40.0
        clk.advance(CORE_HOURS / 40.0)
        gates = TOTAL_GATES
        feature_h = sum(WRONG_FEATURES.values())
        clk.advance((feature_h + LAYERS[7].hours + PLANNED_REFACTOR_H) / 40.0)
        rework = PLANNED_REFACTOR_H
        total = CORE_HOURS + feature_h + LAYERS[7].hours + PLANNED_REFACTOR_H
    return {"order": order, "launch_week": weeks, "gates": gates, "rework_h": rework,
            "total_h": total, "clock": clk.stats()}


def _render_roadmap() -> None:
    """把 8 层的路线图打印成"前置依赖 / 缺失故障 / 验收门槛"的表。"""
    for i, x in enumerate(LAYERS, 1):
        print(f"\n  L{i}  {x.name}   建议投入 {x.hours:.0f} 人时 · 交付门禁 {x.gates} 个")
        note(f"前置依赖 : {x.needs}")
        note(f"缺失故障 : {x.failure}")
        note(f"验收门槛 : {x.dod}")
        note(f"动作清单 : {x.action}")


def main() -> int:
    with lab(LAB_ID, "从 0 搭建生产级 Agent 平台：核心落地的工程顺序",
             "从 0 搭建一个生产级的企业级 agent 平台，核心落地的工程顺序是什么？"):
        phase("1. 复现故障", "(错误顺序：先做花哨编排，再补地基)")
        wrong = simulate_order("wrong")
        blind = _blind_attribution()
        kv("错误顺序上线时间", f"{wrong['launch_week']:.1f}", " 周（虚拟时钟）")
        kv("上线时覆盖的发布门禁", f"{wrong['gates']}/{TOTAL_GATES}", " 个")
        kv("归因过程(无 trace)", f"逐个阶段加打点、发布、复现 {blind['probes']} 轮",
           f"，耗时 {blind['mttr_ms']:.0f}ms")
        print(f"\n{BROKEN} 错误顺序：第 {wrong['launch_week']:.1f} 周上线，只覆盖 "
              f"{wrong['gates']}/{TOTAL_GATES} 个发布门禁；一次 LLM 段变慢的故障，没有 trace "
              f"只能猜 {blind['probes']} 轮、归因耗时 {blind['mttr_ms']:.0f}ms（"
              f"生产上就是 {blind['probes']} 次发布 + 复现）")

        phase("2. 观测 / 归因", "(缺了 trace / deadline / 成本账分别会怎样)")
        trace = _trace_attribution()
        trace["tracer"].render_stage_table()
        note(f"有 trace：一轮带 span 的请求 + 读表，归因耗时 {trace['mttr_ms']:.0f}ms，"
             f"直接指出 {trace['hit']} 段（{trace['duration'][trace['hit']]:.0f}ms）")
        dl = _no_deadline_vs_deadline()
        kv("无预算 vs 有预算的等待", f"{dl['wait_ms_before']:.0f}ms -> "
           f"{dl['wait_ms_after']:.0f}ms", "（对端挂死 2s）")
        cost = _cost_attribution()
        kv("账单错摊率(粗摊 -> ledger)", f"{cost['misalloc_pct']:.1f}% -> 0.0%",
           f"（{cost['calls']} 次调用共 ${cost['usd']:.4f}）")
        for tenant, usd in sorted(cost["by_tenant"].items()):
            note(f"{tenant}: ${usd:.5f}")
        print(f"\n{BROKEN} 缺观测/可靠性/成本账的三连击：归因靠猜 {blind['mttr_ms']:.0f}ms、"
              f"一次抖动等满 {dl['wait_ms_before']:.0f}ms、账单错摊 "
              f"{cost['misalloc_pct']:.1f}%（租户模型档位不同，请求数 ≠ 成本）")

        phase("3. 修复", "(分层递进：L1 打点 → L2 可靠性 → … → L8 高级能力)")
        right = simulate_order("right")
        print(f"\n{FIX} 正确顺序：第 {right['launch_week']:.1f} 周上线（先把 L1~L7 做完），"
              f"门禁 {right['gates']}/{TOTAL_GATES} 全覆盖，返工 "
              f"{right['rework_h']:.0f} 人时；错误顺序返工 {wrong['rework_h']:.0f} 人时")
        _render_roadmap()

        phase("4. 验证")
        head("平台能力清单 + 每层验收门槛")
        for i, x in enumerate(LAYERS, 1):
            METRICS.gauge(f"platform_L{i}_ready", x.dod, 1.0)
            METRICS.counter(f"platform_L{i}_gates_total", x.name).inc(x.gates)
        METRICS.counter("platform_gates_covered_wrong_total", "错误顺序上线时覆盖").inc(wrong["gates"])
        METRICS.counter("platform_gates_covered_right_total", "正确顺序上线时覆盖").inc(right["gates"])
        METRICS.render(f"平台能力清单(1=必备) / 门禁共 {TOTAL_GATES} 个",
                       include=["platform_"])
        note(f"归因耗时     : {blind['mttr_ms']:9.0f}ms -> {trace['mttr_ms']:9.0f}ms")
        note(f"返工工时     : {wrong['rework_h']:9.0f}h  -> {right['rework_h']:9.0f}h")
        note(f"上线门禁覆盖 : {wrong['gates']:9d}   -> {right['gates']:9d}")
        print()
        rows = [("mttr_attribution_ms", blind["mttr_ms"], trace["mttr_ms"], "{:.0f}"),
                ("rework_cost_hours", wrong["rework_h"], right["rework_h"], "{:.0f}"),
                ("gate_coverage_pct", wrong["gates"] / TOTAL_GATES * 100.0,
                 right["gates"] / TOTAL_GATES * 100.0, "{:.0f}"),
                ("no_deadline_wait_ms", dl["wait_ms_before"], dl["wait_ms_after"], "{:.0f}"),
                ("cost_misattribution_pct", cost["misalloc_pct"], 0.0, "{:.1f}")]
        for name, before, after, fmt in rows:
            print(f"{VERIFY} {name}: {fmt.format(before)} -> {fmt.format(after)} "
                  f"({improvement(before, after)})")
        print()
        note("估算依据（rework_cost_hours）：错误顺序 = 事故定位止血 3×12h + 事故压力下补")
        note(f"  L1~L7 的溢价 {CORE_HOURS:.0f}h×25% + 给已建功能补打点/可靠性 "
             f"{sum(WRONG_FEATURES.values()):.0f}h×35%；")
        note(f"  正确顺序 = 预留重构 {PLANNED_REFACTOR_H:.0f}h。")
        note("工程结论：")
        note("1) 顺序 = 依赖顺序：L1 观测是 L2 可靠性的验收工具，L2 是 L3 成本的前提……")
        note("2) 每一层的交付物不是「功能」，而是「下一层能用的度量与门禁」。")
        note("3) 缺了 L1/L2 不是「晚点做」，而是要用返工 + 事故来买（本 lab 量化了价格）。")
        note("4) 高级能力（多智能体）永远排在最后：它是复杂度的放大器，不是地基。")
        takeaway("先建观测与可靠性，再建能力；顺序错了，省下的时间会以返工和事故的形式还回去。")
        METRICS.reset()
    return 0


QUESTIONS = [
    "从 0 搭建生产级 agent 平台的核心工程顺序？ -> L1 单请求链路+打点 → L2 可靠性原语 → "
    "L3 缓存与成本 → L4 上下文与状态 → L5 权限与多租户 → L6 队列与长任务 → "
    "L7 评估与发布门禁 → L8 多智能体/高级能力",
    "为什么不能先做多智能体编排？ -> 没有 trace/预算/成本账时故障无法定位、账单无法归因、"
    "无法回滚；本 lab 用虚拟时钟量化了返工工时（错误顺序 vs 正确顺序）",
    "每一层的验收门槛是什么？ -> 见 LAYERS 表：每层都给出可度量的 DoD 与它交付的发布门禁数",
]
if __name__ == "__main__":
    sys.exit(main())
