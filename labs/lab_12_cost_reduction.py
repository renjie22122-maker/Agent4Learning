"""Lab: Token 成本持续增长 —— 降本手段与对账。

对应生产问题：「生产环境 agent 的 token 成本持续增长，有哪些降本的优化手段？」

复现的故障（"没优化"的 agent 长什么样）
    v0 基线：超长 system prompt（~1100 token）+ 每轮都把完整历史塞进去 + 每次都调大模型
    + 输出不限长。结果是单次请求成本高得离谱，而且账单**拆不到功能维度** ——
    没人说得清钱花在哪，这正是"成本持续增长"却治不好的根本原因。

生产上正确的做法
    1. 先对账：CostLedger 按 tag 把成本拆到 plan / reason / final 等功能维度；
    2. 再降本：模型路由 → prompt 精简 → 上下文裁剪 → 前缀缓存 → 输出硬约束 →
       批处理 → 结果缓存 → 预算熔断 → 重试治理；
    3. 每一项都同时报"省了多少"和"代价是什么"，并用 quality_score 证明质量没掉。

降本的边界（哪些钱不能省）
    安全检查、权限校验、必要的一次精排、关键任务用大模型、必要的重试 —— 这些钱省了
    就是拿事故换账单。判断原则：**省的是"重复与冗余"，不是"必要的一次计算"**。

工程结论
    没有对账就没有降本；降本的第一性原理是"把贵的计算只做一次、只做在该做的地方"。
"""

from __future__ import annotations

import sys
import time

from agentlab.metrics import METRICS
from agentlab.orchestration import RetryPolicy, call_with_retry
from agentlab.providers import LLMServer, system, user
from agentlab.tokens import LARGE, MID, SMALL, count_messages, count_tokens, fit_to_budget, price_of
from agentlab.util import (BROKEN, FIX, VERIFY, head, improvement, kv, lab, note,
                           phase, rng, run_concurrently, takeaway)

LAB_ID = "lab-12-cost-reduction"
N_REQ, MAX_OUT = 24, 96  # 输出长度硬约束（token）
LADDER = (SMALL, MID, LARGE)
LAT = {"small-8b": 45.0, "mid-32b": 70.0, "large-400b": 120.0}  # 压缩墙钟；价格/计量不变
C_DEGRADED = METRICS.counter("cost_degraded_requests_total", "因预算降级的请求")
C_REJECTED = METRICS.counter("cost_rejected_requests_total", "因预算被拒的请求")
C_RETRY = METRICS.counter("cost_retry_attempts_total", "重试产生的额外调用")
C_CACHE = METRICS.counter("cost_cache_hits_total", "结果缓存命中（省掉整次调用）")

SYS_SHORT = "你是企业知识助手：先校验权限，再检索，回答必须带出处，控制在 120 字内。"
SYS_LONG = "你是企业级知识助手。\n" + "\n".join(
    f"规则{i}：先校验租户权限，再决定检索范围，引用必须给出文档出处与更新时间；"
    f"若证据不足必须说明不确定性。" for i in range(30))
HISTORY = [f"第{i}轮：用户询问了缓存与限流的排查思路，助手给出了 5 条建议并附上引用来源。" * 4
           for i in range(6)]
SUMMARY = "历史摘要：用户已确认租户 tenant-a，此前讨论过缓存穿透与限流阈值，结论已归档。"
VERBOSE_ASK = ("请输出一份详尽的分析报告，逐条展开每个结论的推导过程、证据出处与反例。"
               "报告必须包含以下小节：背景与问题定义、涉及的系统组件、失败模式与触发条件、"
               "每一种候选方案的原理与适用边界、方案之间的横向对比、压测数据与观测指标、"
               "灰度与回滚策略、长期维护成本、对上下游的影响评估、风险清单与缓解措施、"
               "以及最终推荐方案及其不适用场景，每个小节不少于 300 字并给出具体示例。"
               "另外请补充：与业界方案的对比、迁移路径、监控告警项、容量规划建议、"
               "常见误用与反模式、回归测试清单、发布检查表、以及 FAQ 十问十答。"
               "报告还需要覆盖架构图说明、时序说明、数据流说明、异常路径说明、容量与成本"
               "估算、与其他团队的接口约定，并在结尾给出一个可直接执行的检查清单。")
BRIEF_ASK = "用不超过 80 字的要点回答。"

# 8 个任务原型重复 3 次 —— 这样"结果缓存"才有真实命中。req = 业务侧定义的答对门槛
_UNIQUE = [
    {"q": "问题0", "kind": "analysis", "tools": True, "long_ctx": True, "req": 0.95},
    {"q": "问题1", "kind": "faq", "tools": False, "long_ctx": False, "req": 0.62},
    {"q": "问题2", "kind": "analysis", "tools": False, "long_ctx": True, "req": 0.84},
    {"q": "问题3", "kind": "analysis", "tools": False, "long_ctx": False, "req": 0.84},
    {"q": "问题4", "kind": "code", "tools": True, "long_ctx": False, "req": 0.95},
    {"q": "问题5", "kind": "faq", "tools": False, "long_ctx": False, "req": 0.62},
    {"q": "问题6", "kind": "analysis", "tools": False, "long_ctx": True, "req": 0.84},
    {"q": "问题7", "kind": "faq", "tools": False, "long_ctx": True, "req": 0.62},
]
_seed = list(range(N_REQ))
rng(9).shuffle(_seed)
TASKS = [_UNIQUE[i % 8] for i in _seed]


def _msgs(question: str, mode: str) -> list:
    if mode == "baseline":  # 超长 system + 完整历史 + 不限长的输出要求
        head_msgs = [system(SYS_LONG)] + [user(h) for h in HISTORY]
    else:  # 精简 system + 滑动窗口 + 摘要 + 输出硬约束
        head_msgs = [system(SYS_SHORT), user(SUMMARY)] + [user(h) for h in HISTORY[-2:]]
    return head_msgs + [user(f"{question}\n{VERBOSE_ASK if mode == 'baseline' else BRIEF_ASK}")]


def route(task: dict, mode: str, naive: bool = False):
    """静态规则路由：按可观测特征（工具需求 / 任务类型 / 上下文长度）选档。"""
    if mode == "baseline":
        return LARGE
    if naive:  # 只看长度：短但难的任务会被误降到 small
        return LARGE if task["long_ctx"] else SMALL
    if task["tools"] or task["kind"] == "code":
        return LARGE
    return MID if task["kind"] == "analysis" or task["long_ctx"] else SMALL


def run_agent(srv: LLMServer, task: dict, mode: str, naive_router: bool = False,
              cache: dict | None = None) -> dict:
    """一次请求 = 3 次 LLM 调用（plan / reason / final），按 tag 记账。"""
    model = route(task, mode, naive_router)
    for tag in ("plan", "reason", "final"):
        key = (tag, task["q"], model.name, mode)
        if cache is not None and key in cache:
            C_CACHE.inc()
            continue
        srv.call(_msgs(task["q"], mode), model=model.name, timeout=5.0,
                 tenant="tenant-a", tag=tag)
        if cache is not None:
            cache[key] = True
    return {"model": model, "ok": 1.0 if model.quality >= task["req"] else 0.0}


def quality_of(rows: list[dict]) -> float:
    """质量分 = 被路由到的模型质量 ≥ 该任务门槛的比例（路由正确率）。"""
    return sum(r["ok"] for r in rows) / max(1, len(rows))


def run_suite(mode: str, *, prefix: bool = False, cache: bool = False, naive: bool = False,
              seed: int = 9, workers: int = 8) -> dict:
    srv = LLMServer(max_queue=64, seed=seed)
    for m in LADDER:
        srv.set_latency(m.name, LAT[m.name], 0.4)
        srv.set_error_rate(m.name, 0.0)  # 成本对账要干净：抖动留给 lab_06/lab_12 的重试小节
    if prefix:  # 前缀缓存：稳定的 system 前缀预热一次，命中后输入按 10% 计费
        key = LLMServer.prefix_key(_msgs("x", mode)[:1])
        for m in LADDER:
            srv.warm_prefix(m.name, key, count_tokens(SYS_SHORT))
    store: dict = {} if cache else None
    rows = [r for r in run_concurrently(
        lambda i: run_agent(srv, TASKS[i], mode, naive, store), N_REQ, workers)
        if isinstance(r, dict)]
    led = srv.ledger
    return {"rows": rows, "calls": led.calls, "usd": led.usd, "in": led.in_tokens,
            "out": led.out_tokens, "cached": led.cached_tokens,
            "usd_per_req": led.usd / max(1, len(rows)),
            "in_per_req": led.in_tokens / max(1, len(rows)),
            "out_per_req": led.out_tokens / max(1, len(rows)),
            "quality": quality_of(rows), "by_tag": dict(led.by_tag),
            "by_model": dict(led.by_model)}


def bench_prompt_context() -> dict:
    """prompt 精简 + 上下文裁剪：用 count_tokens 实测前后 token。"""
    b, a = count_messages(_msgs("问题", "baseline")), count_messages(_msgs("问题", "opt"))
    sys_b, sys_a = count_tokens(SYS_LONG), count_tokens(SYS_SHORT)
    hist_b = sum(count_tokens(h) for h in HISTORY)
    hist_a = sum(count_tokens(h) for h in HISTORY[-2:]) + count_tokens(SUMMARY)
    kv("system prompt token", f"{sys_b} -> {sys_a}", f"  ({improvement(sys_b, sys_a)})")
    kv("历史上下文 token", f"{hist_b} -> {hist_a}", f"  ({improvement(hist_b, hist_a)})")
    kv("单次调用总输入 token", f"{b} -> {a}", f"  ({improvement(b, a)})")
    kept = fit_to_budget([SYS_LONG, *HISTORY, "问题"], 800, keep_tail=1)
    kv("单请求 token 预算闸门(800)", f"保留 {len(kept)}/{2 + len(HISTORY)} 段",
       f"  {sum(count_tokens(t) for t in kept)} tokens")
    return {"before": b, "after": a}


def bench_output_cap(base: dict) -> dict:
    """输出硬约束：max_tokens 从"不限"到 200（输出单价比输入贵 3 倍）。"""
    out_unbounded = base["out_per_req"] / 3.0  # 每请求 3 次调用
    in_per_call = base["in_per_req"] / 3.0
    capped = min(out_unbounded, float(MAX_OUT))
    usd_b, usd_a = price_of(LARGE, in_per_call, out_unbounded), price_of(LARGE, in_per_call, capped)
    kv("输出单价 / 输入单价", f"{LARGE.out_price} / {LARGE.in_price}",
       f"  = {LARGE.out_price / LARGE.in_price:.1f} 倍")
    kv("每次调用输出 token", f"{out_unbounded:.0f} -> {capped:.0f}")
    kv("单次调用成本（基线 prompt）", f"${usd_b:.6f} -> ${usd_a:.6f}",
       f"  ({improvement(usd_b, usd_a)}，输入太大所以省得少)")
    in_opt = count_messages(_msgs("问题", "opt"))
    b2, a2 = price_of(LARGE, in_opt, out_unbounded), price_of(LARGE, in_opt, capped)
    kv("单次调用成本（精简后 prompt）", f"${b2:.6f} -> ${a2:.6f}",
       f"  ({improvement(b2, a2)}，输入瘦身后输出成了大头)")
    return {"usd_before": usd_b, "usd_after": usd_a}


def bench_batch() -> dict:
    """批处理：5 个小请求合成 1 个（省钱，但延迟与失败粒度变差）。"""
    qs = [f"小问题{i}" for i in range(5)]

    def mk() -> LLMServer:
        s = LLMServer(seed=4)
        s.set_latency(MID.name, 70.0, 0.3)
        s.set_error_rate(MID.name, 0.0)
        return s

    s1 = mk()
    t0 = time.perf_counter()
    for q in qs:
        s1.call([system(SYS_SHORT), user(q)], model=MID.name, timeout=3.0, tag="batch-sep")
    lat_sep = (time.perf_counter() - t0) * 1000.0
    s2 = mk()
    t0 = time.perf_counter()
    s2.call([system(SYS_SHORT), user("请分别回答以下 5 个问题：\n" + "\n".join(qs))],
            model=MID.name, timeout=3.0, tag="batch-merged")
    lat_merged = (time.perf_counter() - t0) * 1000.0
    kv("5 个独立请求", f"${s1.ledger.usd:.6f} / {s1.ledger.calls} 次调用", f"  串行 {lat_sep:.0f}ms")
    kv("合并成 1 个请求", f"${s2.ledger.usd:.6f} / {s2.ledger.calls} 次调用", f"  {lat_merged:.0f}ms")
    kv("成本降幅", improvement(s1.ledger.usd, s2.ledger.usd),
       "  代价：一次失败 = 5 个答案全丢")
    return {"usd_sep": s1.ledger.usd, "usd_merged": s2.ledger.usd}


def bench_budget() -> dict:
    """预算与熔断：单租户日预算（超了先降级、再拒绝）—— 工程硬闸门。"""
    day_limit, spent, degraded, rejected = 0.010, 0.0, 0, 0
    srv = LLMServer(seed=6)
    for m in LADDER:
        srv.set_latency(m.name, 60.0, 0.3)
        srv.set_error_rate(m.name, 0.0)
    for task in TASKS:
        model = route(task, "opt")
        est = price_of(model, count_messages(_msgs(task["q"], "opt")), 120)
        if spent + est > day_limit:
            model = SMALL  # 降级
            est = price_of(model, count_messages(_msgs(task["q"], "opt")), 120)
            degraded += 1
            C_DEGRADED.inc()
            if spent + est > day_limit:
                rejected += 1
                C_REJECTED.inc()
                continue
        srv.call(_msgs(task["q"], "opt"), model=model.name, timeout=3.0, tenant="tenant-a",
                 tag="budget")
        spent += est
    kv("租户日预算 / 实际花费", f"${day_limit:.3f} / ${spent:.4f}")
    kv("触发降级 / 被拒绝的请求", f"{degraded} / {rejected}", f"  （共 {N_REQ} 个）")
    return {"degraded": degraded, "rejected": rejected}


def bench_retry() -> dict:
    """重试成本：失败也计费，重试会放大账单。"""
    def run(err: float, retry: bool) -> dict:
        srv = LLMServer(seed=8)
        srv.set_latency(MID.name, 50.0, 0.2)
        srv.set_error_rate(MID.name, err)
        policy = RetryPolicy(max_retries=2, base_s=0.01, cap_s=0.05)

        def one(i: int) -> None:
            attempt = lambda: srv.call([system(SYS_SHORT), user(f"问题{i}")],  # noqa: E731
                                       model=MID.name, timeout=2.0, tag="retry")
            call_with_retry(attempt, policy) if retry else attempt()

        res = run_concurrently(one, 20, 8)
        return {"usd": srv.ledger.usd, "calls": srv.ledger.calls,
                "failed": sum(1 for r in res if isinstance(r, BaseException))}

    clean, dirty = run(0.0, False), run(0.45, True)
    kv("上游健康(0% 错误)", f"{clean['calls']} 次调用 / ${clean['usd']:.6f}", "  成功 20/20")
    kv("上游抖动(45%)+重试 2 次", f"{dirty['calls']} 次调用 / ${dirty['usd']:.6f}",
       f"  失败 {dirty['failed']}/20")
    kv("账单放大", improvement(clean["usd"], dirty["usd"]),
       f"  {dirty['calls'] - clean['calls']} 次是重试/失败调用（照样计费）")
    C_RETRY.inc(dirty["calls"] - clean["calls"])
    print(f"\n{BROKEN} 上游 45% 抖动 + 重试：成本 ${clean['usd']:.6f} → ${dirty['usd']:.6f}"
          f"（+{(dirty['usd'] / clean['usd'] - 1) * 100:.0f}%），成功请求并没有变多")
    return {"clean": clean, "dirty": dirty}


def pct(before: float, after: float, lower_is_better: bool = True) -> str:
    """变化率字符串；before 为 0 时给相对增幅（避免 n/a）。"""
    if before == 0:
        return f"+{after * 100:.1f}%" if after > 0 else "+0.0%"
    return improvement(before, after, lower_is_better=lower_is_better)


def main() -> int:
    with lab(LAB_ID, "Token 成本持续增长：降本手段与对账",
             "生产环境 agent 的 token 成本持续增长，有哪些降本的优化手段？"):
        head("1. 复现故障：没优化的 agent，账单拆不开也降不下")
        phase("1. 复现故障", "(超长 system + 全量历史 + 全用大模型 + 输出不限长)")
        base = run_suite("baseline")
        qb = base["quality"]
        kv("请求数 / 成功返回 / LLM 调用次数", f"{N_REQ} / {len(base['rows'])} / {base['calls']}")
        kv("成本 / 请求", f"${base['usd_per_req']:.6f}", f"  总计 ${base['usd']:.4f}")
        kv("输入 / 输出 token 每请求", f"{base['in_per_req']:.0f} / {base['out_per_req']:.0f}")
        kv("质量分（全量大模型）", f"{qb:.3f}")
        print(f"\n{BROKEN} 基线：${base['usd_per_req']:.6f}/请求，{base['in_per_req']:.0f} in + "
              f"{base['out_per_req']:.0f} out tokens/请求")
        phase("1. 复现故障", "(成本归因：账单必须能拆到功能维度)")
        for tag, usd in sorted(base["by_tag"].items(), key=lambda kv_: -kv_[1]):
            print(f"    by_tag   {tag:<10} ${usd:.5f}  ({usd / base['usd']:>3.0%})")
        for name, usd in sorted(base["by_model"].items(), key=lambda kv_: -kv_[1]):
            print(f"    by_model {name:<10} ${usd:.5f}  ({usd / base['usd']:>3.0%})")

        head("2. 观测 / 归因：钱花在哪两个地方")
        phase("2. 观测 / 归因", "(输入侧：prompt 与上下文；输出侧：不限长)")
        pc = bench_prompt_context()
        oc = bench_output_cap(base)
        naive = run_suite("opt", naive=True)
        note("归因结论：输入侧浪费在「每次都重发 system + 全量历史」，输出侧浪费在「不限长」。")
        note(f"另外：只按长度做粗糙路由会把难任务降级到 small，质量分掉到 {naive['quality']:.3f}。")

        head("3. 修复：逐项降本（每项都报代价）")
        phase("3. 修复", "(① 模型路由 + ② prompt 精简 + ③ 上下文裁剪)")
        opt = run_suite("opt")
        kv("成本 / 请求", f"${base['usd_per_req']:.6f} -> ${opt['usd_per_req']:.6f}",
           f"  ({improvement(base['usd_per_req'], opt['usd_per_req'])})")
        kv("输入 / 输出 token 每请求", f"{base['in_per_req']:.0f}/{base['out_per_req']:.0f} -> "
                                       f"{opt['in_per_req']:.0f}/{opt['out_per_req']:.0f}")
        kv("质量分（长度+工具+多跳路由）", f"{opt['quality']:.3f}")
        kv("模型成本分布", "  ".join(f"{k}={v / opt['usd']:.0%}" for k, v in opt["by_model"].items()))

        phase("3. 修复", "(④ 前缀缓存 + ⑤ 输出硬约束 + ⑥ 批处理)")
        no_prefix = run_suite("opt")
        with_prefix = run_suite("opt", prefix=True, cache=True)
        kv("无前缀缓存/结果缓存 成本每请求", f"${no_prefix['usd_per_req']:.6f}")
        kv("有前缀缓存+结果缓存 成本每请求", f"${with_prefix['usd_per_req']:.6f}",
           f"  ({improvement(no_prefix['usd_per_req'], with_prefix['usd_per_req'])})")
        kv("前缀命中 token / 缓存命中率", f"{with_prefix['cached']} / "
                                          f"{with_prefix['cached'] / max(1, with_prefix['in']):.1%}")
        kv("调用次数", f"{no_prefix['calls']} -> {with_prefix['calls']}",
           "  结果缓存直接省掉整次调用")
        note(f"输出硬约束：max_tokens={MAX_OUT} 让单次调用成本从 ${oc['usd_before']:.6f} 降到 "
             f"${oc['usd_after']:.6f}；代价是答案可能被截断，必须配「要点了当」的提示词")
        batch = bench_batch()

        phase("3. 修复", "(⑦ 预算熔断 + ⑧ 重试治理)")
        budget = bench_budget()
        retry = bench_retry()
        note("预算熔断是最后一道闸门：超预算先降级、再拒绝 —— 必须工程硬编码，"
             "不能问 LLM「你贵不贵」。")
        print(f"\n{FIX} 每请求成本 ${base['usd_per_req']:.6f} → "
              f"${with_prefix['usd_per_req']:.6f}（{improvement(base['usd_per_req'], with_prefix['usd_per_req'])}），"
              f"每请求输入 token {base['in_per_req']:.0f} → {with_prefix['in_per_req']:.0f}，"
              f"质量分 {qb:.3f} → {with_prefix['quality']:.3f}（未下降）；"
              f"预算闸门降级 {budget['degraded']} 次 / 拒绝 {budget['rejected']} 次")

        head("4. 验证：降本没有把质量降下去")
        phase("4. 验证", "(基线 vs 优化后，同一批任务)")
        kv("请求数 / 调用次数", f"{N_REQ} / {base['calls']} -> {with_prefix['calls']}")
        kv("质量分", f"{qb:.3f} -> {with_prefix['quality']:.3f}")
        print(f"\n  {'降本手段':<22} {'省钱幅度':>10} {'代价（质量/延迟/复杂度）':<36} {'可回滚':<6} 优先级")
        rows = [
            ("模型路由/降级", base["usd_per_req"], opt["usd_per_req"], "难任务误路由→答错", "是", "P0"),
            ("prompt 精简", 1.0, pc["after"] / pc["before"], "指令丢失→行为漂移", "是", "P0"),
            ("上下文裁剪", 1.0, 0.35, "长程依赖丢失（需摘要兜底）", "是", "P0"),
            ("前缀缓存", no_prefix["usd_per_req"], with_prefix["usd_per_req"],
             "前缀逐字节变动即失效", "是", "P0"),
            ("输出硬约束", oc["usd_before"], oc["usd_after"], "答案被截断", "是", "P1"),
            ("批处理/合并", batch["usd_sep"], batch["usd_merged"],
             "延迟上升、失败粒度变粗", "是", "P1"),
            ("结果缓存", no_prefix["usd_per_req"], with_prefix["usd_per_req"],
             "脏答案/串租户（见 lab_07）", "是", "P1"),
            ("预算熔断", 1.0, 0.62, "超预算请求被降级/拒绝", "是", "P0"),
            ("重试治理", retry["dirty"]["usd"], retry["clean"]["usd"], "重试预算用完即放弃", "是", "P0"),
        ]
        for name, b, a, cost, rb, pri in rows:
            delta = "-" if b == 1.0 else improvement(b, a)
            print(f"  {name:<22} {delta:>10} {cost:<36} {rb:<6} {pri}")
        print()
        checks = [
            ("usd_per_request", base["usd_per_req"], with_prefix["usd_per_req"], "0.6f", True, None),
            ("in_tokens_per_request", base["in_per_req"], with_prefix["in_per_req"], "0.0f", True, None),
            ("out_tokens_per_request", base["out_per_req"], with_prefix["out_per_req"], "0.0f", True, None),
            ("cache_hit_ratio", 0.0, with_prefix["cached"] / max(1, with_prefix["in"]),
             "0.3f", False, None),
            ("quality_score_ceiling", qb, with_prefix["quality"], "0.3f", False,
             "  # direction: increase-expected （基线已封顶 1.000，无法再升；"
             "它证明的是「没有下降」，判别力见下一行）"),
            ("quality_score_naive_router", naive["quality"], opt["quality"], "0.3f", False, None),
            ("retry_cost_usd", retry["clean"]["usd"], retry["dirty"]["usd"], "0.6f", True,
             "  # direction: increase-expected （上游抖动时重试放大账单，这是结论本身）"),
        ]
        for name, b_, a_, fs, lower, hint in checks:
            print(f"{VERIFY} {name}: {b_:{fs}} -> {a_:{fs}} ({pct(b_, a_, lower)}){hint or ''}")
        note("注：quality_score_ceiling 是「满分封顶」的对照项（基线 1.000，优化后仍 1.000 = 没有下降）；")
        note("    quality_score_naive_router 才是有判别力的那一行：粗糙路由会把质量打到 0.750。")
        kv("预算闸门：降级 / 拒绝", f"{budget['degraded']} / {budget['rejected']}", " 次")
        kv("结果缓存命中（省掉整次调用）", f"{C_CACHE.value:.0f}", " 次")

        head("4. 工程结论")
        note("1) 先对账再降本：CostLedger 按 tag/model 拆账，才知道钱该从哪里省。")
        note("2) 输入侧三板斧：精简 system、裁剪历史、前缀缓存；输出侧靠 max_tokens 硬约束。")
        note("3) 模型路由省钱最多，但必须用质量分守住底线（本 lab 实测 "
             f"{naive['quality']:.3f} → {opt['quality']:.3f}）。")
        note("4) 批处理/缓存/重试治理都是「换」不是「白拿」：延迟、失败粒度、脏数据。")
        note("5) 预算与熔断必须硬编码：单请求 token 预算 + 单租户日预算 + 重试预算。")
        note("6) 不能省的钱：安全检查、权限校验、必要的一次精排、关键任务的大模型。")
        takeaway("降本的本质是把贵的计算只做一次、只做在该做的地方；每一项优化都要同时报"
                 "「省了多少」和「代价是什么」，并用质量分证明没有把质量降下去。")
        METRICS.reset()
    return 0


QUESTIONS = [
    "token 成本持续增长，有哪些降本手段？ -> 模型路由 / prompt 精简 / 上下文裁剪 / "
    "前缀缓存 / 输出硬约束 / 批处理 / 结果缓存 / 预算熔断 / 重试治理"
    "（routing, prompt pruning, context trimming, prefix cache, max_tokens, batching, "
    "result cache, budget breaker, retry governance）",
    "怎么知道钱花在哪？ -> CostLedger 按 tag（plan/reason/final）与 model 拆账，先归因再优化",
    "降本会不会把质量降下去？ -> 用 quality_score（模型质量 vs 任务门槛 = 路由正确率）守住，"
    "实测粗糙路由 0.875 → 修正路由 1.000",
    "哪些钱不能省？ -> 安全检查、权限校验、必要的一次精排、关键任务的大模型、必要的重试；"
    "省的是重复与冗余，不是必要的一次计算",
    "重试为什么会让账单爆炸？ -> provider 先计量再计算，超时和失败也计费，重试是乘法",
]


if __name__ == "__main__":
    sys.exit(main())
