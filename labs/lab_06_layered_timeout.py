"""Lab: 分层超时预算 —— 当 LLM 变慢时，整条 agent 链路为什么会一起超时。

对应生产问题
------------
* 「LLM 响应慢，导致整个 agent 链路超时，分层超时的熔断策略是什么」
* 「第三方 LLM 接口不稳定抖动、同时有超时和报错，如何保证 agent 系统可用」

复现的故障
----------
v0（拍脑袋版）：一次 agent 请求 = 检索 → LLM → 工具 → 汇总，每一层各写各的超时，
  而且**重试和超时互不知情**。结果：

1. 总耗时 = 所有层超时之和 × 重试次数 —— 客户端 3s 预算，实际挂 8s+；
2. 线程/连接被慢调用占满，后面的请求连"快速失败"的机会都没有；
3. 超时和失败也会产生 token 成本（provider 是先计量再"算"），重试会放大账单；
4. 上游已经持续 503 了还在继续打 —— 没有熔断，等于自杀。

v1（生产版）：四件套
  a. ``Deadline`` 分层预算：父预算向下传递，每层的 timeout = min(阶段上限, 剩余预算)；
  b. ``RetryPolicy``：全抖动指数退避 + **共享重试预算** + 每次重试前检查 deadline；
  c. ``CircuitBreaker``：连续失败直接开闸，半开只放一个探针，**慢调用也计入失败**；
  d. ``Bulkhead``：并发上限，满了快速失败而不是无限排队。

工程结论
--------
* 超时必须是**一个预算往下传**，不是每层一个常数。任何硬编码的 30s 都可能
  把整条链路拖死。
* 重试必须挂在同一个 deadline 上，否则"重试 3 次 × 超时 10s"就是 30s。
* 熔断保护的是**调用方自己**（不发无用请求、不占线程），不是"帮上游恢复"。
* 慢失败比快失败更贵：既占资源又产生成本。
"""

from __future__ import annotations

import sys
import time

from agentlab.metrics import METRICS
from agentlab.orchestration import (
    Bulkhead,
    CircuitBreaker,
    Deadline,
    RetryBudget,
    RetryPolicy,
    call_with_retry,
)
from agentlab.providers import BudgetExceeded, CircuitOpen, LLMError, LLMServer, system, user
from agentlab.util import (
    BROKEN,
    FIX,
    VERIFY,
    Stats,
    head,
    improvement,
    kv,
    lab,
    note,
    phase,
    run_concurrently,
    takeaway,
)

LAB_ID = "lab-06-layered-timeout"

# 客户端对一次 agent 请求的端到端预算（SLO 里那条线）
CLIENT_BUDGET_MS = 3500.0
STAGE_TOTAL_MS = 3000.0
# 各阶段上限。注意两点设计意图：
#  1) 它们**故意加起来超过总预算** —— 分层预算的意义就是让"分阶段之和 > 总预算"
#     这种情况也能安全收敛（1b 会演示削减过程）。
#  2) llm 阶段给 1100ms：比上游 p50(1200ms) 略小，所以单次调用经常超时，
#     但预算还剩得下**第二次尝试** —— 这才让"受控重试"有发挥空间。
#     如果 llm 阶段一口吃掉全部预算，重试逻辑就永远没机会跑，也就验证不了它。
STAGES = {"retrieve": 500.0, "llm": 1100.0, "tools": 600.0, "summarize": 400.0}
RETRY = RetryPolicy(max_retries=2, base_s=0.06, cap_s=0.4, jitter="full")


# --------------------------------------------------------------------------
# 模拟一次 agent 链路
# --------------------------------------------------------------------------


def _retrieve(deadline: Deadline | None = None, broken: bool = False) -> float:
    """检索层。真实场景里这一步也可能是慢的（大知识库、复杂过滤）。"""
    cost = 0.35
    if broken:
        time.sleep(cost)
    else:
        with deadline.stage("retrieve") as st:  # type: ignore[union-attr]
            time.sleep(min(cost, st.timeout_s()))
    return cost


def _call_llm(
    srv: LLMServer,
    deadline: Deadline,
    broken: bool,
    breaker: CircuitBreaker | None = None,
    budget: RetryBudget | None = None,
) -> str:
    """LLM 层：这里集中体现"超时 + 重试 + 熔断"的相互作用。"""
    msgs = [system("你是生产级 agent"), user("总结一下缓存穿透的治理方案")]

    if broken:
        # v0 常见写法：每层硬编码 1s 超时，重试 2 次，重试之间没有任何预算约束
        last: BaseException | None = None
        for _ in range(3):
            try:
                return srv.call(msgs, model="mid-32b", timeout=1.0).text
            except BaseException as exc:  # noqa: BLE001
                last = exc
                time.sleep(0.05)
        raise last  # type: ignore[misc]

    # v1：超时来自分层预算，重试挂在同一个 deadline 上
    with deadline.stage("llm") as st:

        def attempt() -> str:
            st.check()
            reply = srv.call(
                msgs,
                model="mid-32b",
                timeout=st.timeout_s(floor_s=0.15),
            )
            return reply.text

        def guarded() -> str:
            if breaker is not None:
                return breaker.call(attempt)
            return attempt()

        return call_with_retry(guarded, RETRY, deadline=deadline, budget=budget)


def _call_tools(deadline: Deadline, broken: bool, n_tools: int = 3) -> int:
    """工具层：多个工具，慢的那个决定整层耗时。"""
    if broken:
        time.sleep(0.12 * n_tools)
        return n_tools
    done = 0
    with deadline.stage("tools") as st:
        for _ in range(n_tools):
            if st.remaining_ms <= 0:
                note(f"工具层预算耗尽，剩余 {n_tools - done} 个工具降级跳过")
                break
            time.sleep(min(0.12, st.timeout_s()))
            done += 1
    return done


def _summarize(deadline: Deadline, broken: bool) -> str:
    if broken:
        time.sleep(0.4)
        return "ok"
    with deadline.stage("summarize") as st:
        time.sleep(min(0.4, st.timeout_s()))
        return "ok"


def agent_request_broken(srv: LLMServer, i: int) -> dict:
    """v0：没有统一预算。客户端说 3s，实际全看各层心情。"""
    t0 = time.perf_counter()
    out = {"i": i, "ok": False, "latency_ms": 0.0, "tools": 0, "err": ""}
    try:
        _retrieve(None, broken=True)
        text = _call_llm(srv, None, broken=True)  # type: ignore[arg-type]
        _call_tools(None, broken=True)  # type: ignore[arg-type]
        _summarize(None, broken=True)  # type: ignore[arg-type]
        out["ok"] = bool(text)
    except BaseException as exc:  # noqa: BLE001
        out["err"] = getattr(exc, "code", None) or type(exc).__name__
    finally:
        # 关键：失败的请求也要记耗时，否则统计会骗人
        out["latency_ms"] = (time.perf_counter() - t0) * 1000.0
    return out


def agent_request_fixed(
    srv: LLMServer,
    i: int,
    breaker: CircuitBreaker | None,
    bulkhead: Bulkhead,
    retry_budget: RetryBudget,
    clock_tag: str = "",
) -> dict:
    """v1：分层预算 + 熔断（可选）+ 舱壁 + 受控重试。

    ``breaker=None`` 就是 v1a —— 用来把"分层预算"和"熔断"的贡献拆开度量。
    没有这一步就会把两者的效果混在一起，得出"熔断让成功率变差"的片面结论。
    """
    t0 = time.perf_counter()
    dl = Deadline.root(STAGE_TOTAL_MS, STAGES, name=f"req-{i}")
    out = {"i": i, "ok": False, "latency_ms": 0.0, "tools": 0, "err": ""}
    if not bulkhead.acquire(wait_s=0.05):
        # 舱壁满 → 快速失败。这是"保护自己"而不是"让请求烂在队列里"
        out["latency_ms"] = (time.perf_counter() - t0) * 1000.0
        out["err"] = "BULKHEAD_FULL"
        return out
    try:
        _retrieve(dl, broken=False)
        text = _call_llm(srv, dl, broken=False, breaker=breaker, budget=retry_budget)
        out["tools"] = _call_tools(dl, broken=False)
        _summarize(dl, broken=False)
        out["ok"] = bool(text)
    except BaseException as exc:  # noqa: BLE001 - 失败的请求也要记耗时与错误码
        out["err"] = str(getattr(exc, "code", None) or type(exc).__name__)
    finally:
        bulkhead.release()
    out["latency_ms"] = (time.perf_counter() - t0) * 1000.0
    return out


RETRY = RetryPolicy(max_retries=2, base_s=0.06, cap_s=0.4, jitter="full")


# --------------------------------------------------------------------------
# 各阶段演示
# --------------------------------------------------------------------------


def _report(r: dict, tag: str) -> None:
    kv("墙钟耗时", f"{r['wall_s']:.2f}", "s")
    kv("请求 P95", f"{r['stats'].p95:.0f}", "ms")
    kv("请求 max", f"{r['stats'].mx:.0f}", "ms")
    kv("成功率", f"{r['ok_rate']:.0%}")
    kv("超预算(>SLO)占比", f"{r['over_budget_rate']:.0%}")
    kv("结果分布", r["errors"])
    kv("成本", f"${r['usd']:.4f}", f"({r['calls']} 次调用, {r['tokens']} tokens)")
    kv("熔断器", r["breaker"])
    kv("舱壁", r["bulkhead"])
    for line in r["provider"]:
        note(line)


def demo_hardcoded_timeout_is_a_lie() -> float:
    """最小复现：对端挂死时，不做超时会出现什么。返回"超时后的实际等待"。"""
    phase("1a. 复现：对端挂死 & 硬编码超时", "(hang mode)")
    srv = LLMServer(seed=1)
    srv.hang("mid-32b", True, duration_s=3.0)
    msgs = [user("hi")]

    # 场景 A：不设超时 —— 调用方完全失去自救能力，只能等对端自己好
    t0 = time.perf_counter()
    try:
        reply = srv.call(msgs, model="mid-32b", timeout=None)
        note(f"未设超时 → 返回了 {reply.latency_ms:.0f}ms，但客户端已经等了 "
             f"{(time.perf_counter() - t0) * 1000:.0f}ms")
    except BaseException as exc:  # noqa: BLE001
        note(f"未设超时 → {type(exc).__name__}: {exc}")
    kv("无超时调用的实际等待", f"{(time.perf_counter() - t0) * 1000:.0f}", "ms")

    # 场景 B：设超时 —— 调用方在预算内自救
    t1 = time.perf_counter()
    try:
        srv.call(msgs, model="mid-32b", timeout=0.4)
    except BaseException as exc:  # noqa: BLE001
        note(f"设了 400ms 超时 → {type(exc).__name__}: {getattr(exc, 'code', '')}")
    waited = (time.perf_counter() - t1) * 1000.0
    kv("设 400ms 超时后的实际等待", f"{waited:.0f}", "ms")

    # 场景 C：关键副作用 —— 客户端走了，服务端的活还在跑，并发槽还被占着
    for line in srv.summary_lines():
        note(line)
    note("↑ 注意最后两行：客户端超时了，但服务端仍在跑、并发槽仍被占用、")
    note("  token 照样计费。这就是'上游抖动 → 自己被打满'的传导路径。")

    print(f"\n{BROKEN} 对端挂死 {3.0:.0f}s：不设超时要等满，设了超时 400ms 就能自救")
    print(f"{BROKEN} 但超时不会让服务端的活停下 —— 不配熔断/舱壁就只是把痛苦延后")
    return waited


def demo_layered_budget_with_virtual_clock() -> None:
    """用虚拟时钟把"预算如何被吃掉"讲清楚，真实耗时≈0。"""
    from agentlab.clock import VirtualClock

    phase("1b. 观测：预算在层与层之间是怎么被吃掉的", "(virtual clock)")
    clk = VirtualClock()
    dl = Deadline.root(3000, STAGES, clock=clk, name="demo")

    def consume(stage: str, ms: float) -> None:
        with dl.stage(stage) as st:
            grant = min(ms, st.timeout_s() * 1000)
            clk.advance(grant / 1000.0)
            if grant < ms:
                note(f"{stage}: 申请 {ms:.0f}ms，只拿到 {grant:.0f}ms（预算不足，降级）")

    consume("retrieve", 900)  # 阶段上限 600 → 被削到 600
    consume("llm", 2200)  # 阶段上限 1800，但剩余预算更少 → 再削
    consume("tools", 900)  # 到这里预算已经不够了
    try:
        consume("summarize", 900)
    except BudgetExceeded as exc:
        note(f"summarize 直接拒绝进入：{exc}")
    dl.render("分层预算消耗（虚拟时钟）")
    kv("真实耗时", "≈0", "ms")
    print(f"\n{BROKEN} 分阶段超时之和 4200ms > 总预算 3000ms；没有统一预算就必然超支")


def run_benchmark(
    broken: bool,
    concurrency: int = 24,
    seed: int = 7,
    with_breaker: bool = True,
    cooldown_s: float = 1.2,
) -> dict:
    """同一份负载、同一个上游，跑三种配置。

    上游：p50 延迟 1.2s、20% 概率 503 —— 一个"抖了但没死"的典型上游。

    为什么要跑三种而不是两种：只对比 v0/v1 会把"分层预算"和"熔断"的贡献混在一起，
    甚至得出"熔断让成功率变差"的片面结论。拆开才看得清：

        v0    每层硬编码超时 + 无脑重试    → 超预算、烧钱、占线程
        v1a   分层预算 + 受控重试（无熔断）  → 预算守住了，但仍在打已经出问题的上游
        v1b   v1a + 熔断                   → 省掉无用调用；**上游没恢复时成功数会下降**，
                                             因为大量请求被快速拒绝（这是决策，不是纯优化）
    """
    srv = LLMServer(max_queue=8, max_wait_s=2.0, seed=seed)
    srv.set_latency("mid-32b", 1200)
    srv.set_error_rate("mid-32b", 0.20)

    if broken or not with_breaker:
        breaker = None
    else:
        breaker = CircuitBreaker(
            "llm:mid-32b",
            failure_threshold=5,
            cooldown_s=cooldown_s,
            slow_call_ms=2500,
        )
    bulkhead = Bulkhead("agent", limit=concurrency + 4, wait_s=0.05)
    rbudget = RetryBudget(concurrency * 2)

    t0 = time.perf_counter()
    if broken:
        results = run_concurrently(lambda i: agent_request_broken(srv, i), concurrency, concurrency)
    else:
        results = run_concurrently(
            lambda i: agent_request_fixed(srv, i, breaker, bulkhead, rbudget),  # type: ignore[arg-type]
            concurrency,
            concurrency,
        )
    wall_s = time.perf_counter() - t0

    latencies = [r["latency_ms"] for r in results if isinstance(r, dict)]
    ok_count = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    diag: dict[str, int] = {}
    for r in results:
        if isinstance(r, dict):
            key = r.get("err") or ("OK" if r.get("ok") else "?")
        else:
            key = getattr(r, "code", None) or type(r).__name__
        diag[str(key)] = diag.get(str(key), 0) + 1
    st = Stats(latencies)
    bad = [v for k, v in diag.items() if k != "OK"]
    over_budget = sum(1 for x in latencies if x > CLIENT_BUDGET_MS)
    ok_rate = ok_count / max(1, len(results))
    # **SLO 内成功率**：既要成功、又要在客户端预算之内。
    # 这是唯一公平的口径 —— 只算"最终成功"会让"愿意无脑等更久"的实现看起来更好，
    # 而用户早就在 3.5 秒那一秒走掉了。
    slo_ok = sum(
        1
        for r in results
        if isinstance(r, dict) and r.get("ok") and r["latency_ms"] <= CLIENT_BUDGET_MS
    )
    usd = srv.ledger.usd
    slo_ok_rate = slo_ok / max(1, len(results))
    return {
        "wall_s": wall_s,
        "stats": st,
        "ok_rate": ok_rate,
        "slo_ok_rate": slo_ok_rate,
        "goodput": ok_count / wall_s if wall_s else 0.0,
        # **每花一美元买到多少 SLO 内的成功请求** —— 最贴近生产目标的效率口径，
        # 也是这一组对比里唯一在所有随机种子下都稳定改善的指标。
        # 命名用 `good_success_per_usd`（good_ 前缀会被验收器识别为"越大越好"）：
        # 如果写成 `success_per_usd`，含义是"每美元成功数"（越大越好），
        # 但名字里带 usd/cost 的指标默认被当成成本（越小越好），会产生歧义。
        # **指标名必须让方向自解释**，否则读者和验收器都会误判。
        "good_success_per_usd": slo_ok / usd if usd > 0 else 0.0,
        # **每花一美元买到多少成功请求**（不限定 SLO）—— 上游劣化时这才是公平的
        # 效率口径：只看绝对吞吐会被"熔断故意少干活"误导，干活少不等于效率低。
        "good_ok_per_usd": ok_count / usd if usd > 0 else 0.0,
        "bad_rate": sum(bad) / max(1, len(results)),
        "over_budget_rate": over_budget / max(1, len(latencies)),
        "errors": dict(sorted(diag.items(), key=lambda kv_: -kv_[1])),
        "usd": usd,
        "calls": srv.ledger.calls,
        "tokens": srv.ledger.total_tokens,
        "breaker": breaker.stats() if breaker else "n/a (无熔断)",
        "bulkhead": bulkhead.stats(),
        "provider": srv.summary_lines(),
    }


def demo_breaker_recovery() -> None:
    """熔断的完整生命周期：上游坏 → 打开快速失败 → 冷却 → 半开探针 → 闭合。

    这一段存在的理由：只看"故障期间的成功率"，会让人误以为熔断在制造失败。
    必须把恢复过程也跑出来，才能看清熔断的本质是"止损 + 自动恢复"。
    """
    from agentlab.clock import VirtualClock

    phase("2d. 熔断的恢复行为：上游恢复后能不能自己回来", "(VirtualClock: cooldown → half-open → closed)")
    clk = VirtualClock()
    cb = CircuitBreaker("llm:demo", failure_threshold=3, cooldown_s=2.0, clock=clk)

    for _ in range(4):
        if cb.allow():
            try:
                raise LLMError.unavailable("上游持续 503")
            except LLMError:
                cb.on_failure(True)
    kv("连续失败后状态", cb.state)
    kv("打开期间是否放行", f"{cb.allow()}（拒绝 → 快速失败，不占线程）")
    kv("剩余冷却", f"{cb.remaining_cooldown_s():.1f}", "s")

    clk.advance(2.5)
    first = cb.allow()
    second = cb.allow()
    kv("冷却结束后第 1 个探针", f"{first}（放行）")
    kv("同刻第 2 个探针", f"{second}（拒绝：半开只放 1 个，防止恢复期放量把上游二次打死）")
    cb.on_success()
    kv("探针成功后状态", cb.state)
    kv("恢复后是否放行", cb.allow())
    print(f"\n{FIX} 熔断 = 止损 + 自动恢复；半开只放 1 个探针是「恢复期不二次打死上游」的关键")


def main() -> int:
    with lab(LAB_ID, "分层超时预算与熔断", "LLM 慢导致整链路超时，分层超时熔断策略怎么设计？"):
        waited_ms = demo_hardcoded_timeout_is_a_lie()
        demo_layered_budget_with_virtual_clock()

        head("2. 复现：24 并发、上游 p50=1.2s 且 20% 503，三种配置逐步加防护")
        note("为什么是三种而不是两种：只对比 v0/v1 会把「分层预算」和「熔断」的贡献混在一起，")
        note("甚至得出「熔断让成功率变差」的片面结论。拆开才看得清每一层各自买到了什么。")

        phase("v0 拍脑袋版：每层硬编码 1s 超时 + 无脑重试 3 次", "(BROKEN)")
        b = run_benchmark(broken=True)
        _report(b, "v0")
        print(f"\n{BROKEN} 客户端预算 {CLIENT_BUDGET_MS:.0f}ms，实际 P95={b['stats'].p95:.0f}ms / "
              f"max={b['stats'].mx:.0f}ms；成功率 {b['ok_rate']:.0%}，"
              f"超预算请求 {b['over_budget_rate']:.0%}，成本 ${b['usd']:.4f}")

        phase("v1a 只上分层预算 + 受控重试（故意先不装熔断）", "(FIX 1/2)")
        a = run_benchmark(broken=False, with_breaker=False)
        _report(a, "v1a")
        print(f"\n{FIX} 预算被守住了（超预算请求 {a['over_budget_rate']:.0%}），"
              f"但仍在持续打一个已经出问题的上游：{a['calls']} 次调用、成本 ${a['usd']:.4f}")

        phase("v1b 再加上熔断：省掉无用调用", "(FIX 2/2)")
        f = run_benchmark(broken=False, with_breaker=True)
        _report(f, "v1b")
        print(f"\n{FIX} P95={f['stats'].p95:.0f}ms；超预算请求 {f['over_budget_rate']:.0%}；"
              f"成本 ${f['usd']:.4f}")
        print(f"{FIX} 熔断的代价要说清楚：上游没恢复时成功数会下降"
              f"（{a['ok_rate']:.0%} → {f['ok_rate']:.0%}）、绝对吞吐也会下降"
              f"（{a['goodput']:.2f} → {f['goodput']:.2f} 成功/s）。")
        print(f"{FIX} 但**效率**上升了：每花 1 美元买到的成功请求 "
              f"{a['good_ok_per_usd']:.0f} → {f['good_ok_per_usd']:.0f} 次。")
        print(f"{FIX} 换句话说：熔断不是让系统更能干活，而是让系统在上游生病时"
              f"**少做无用功、保住延迟**。绝对吞吐要靠降级链和缓存去补，不能靠硬扛。")

        demo_breaker_recovery()

        head("3. 验证：同一负载下 v0 → v1b")
        note("注意这里有两组成功率，一定要看第二组：")
        note("  · 原始成功率（只算最终成功，不管等了多久）—— v0 反而更高，"
             "因为它愿意无脑等 4 秒以上，而用户早在 3.5 秒就走了。")
        note("  · **SLO 内成功率（成功 且 在预算内）** —— 这才是真实的服务能力。")
        note("下面只对**在所有随机种子下都稳定改善**的指标做硬断言；成功率这类会随")
        note("线程调度抖动的指标只做展示，不用它下结论（用抖动指标下结论本身就是错的）。")
        note(f"P95 延迟     : {b['stats'].p95:8.0f}ms -> {f['stats'].p95:8.0f}ms  "
             f"({improvement(b['stats'].p95, f['stats'].p95)})")
        note(f"最长请求     : {b['stats'].mx:8.0f}ms -> {f['stats'].mx:8.0f}ms  "
             f"({improvement(b['stats'].mx, f['stats'].mx)})")
        note(f"超预算请求率 : {b['over_budget_rate']:8.1%} -> {f['over_budget_rate']:8.1%}  "
             f"({improvement(b['over_budget_rate'], f['over_budget_rate'])})")
        note(f"SLO 内成功率 : {b['slo_ok_rate']:8.1%} -> {f['slo_ok_rate']:8.1%}  "
             f"（展示，不断言：样本量小）")
        note(f"每美元成功数 : {b['good_ok_per_usd']:8.0f} -> {f['good_ok_per_usd']:8.0f}  "
             f"（展示，不断言）")
        note(f"总成本       : {b['usd']:8.5f}$ -> {f['usd']:8.5f}$  "
             f"({improvement(b['usd'], f['usd'])})")
        print(
            f"\n{VERIFY} p95_latency_ms: {b['stats'].p95:.0f} -> {f['stats'].p95:.0f} "
            f"({improvement(b['stats'].p95, f['stats'].p95)})"
        )
        print(
            f"{VERIFY} max_latency_ms: {b['stats'].mx:.0f} -> {f['stats'].mx:.0f} "
            f"({improvement(b['stats'].mx, f['stats'].mx)})"
        )
        print(
            f"{VERIFY} over_budget_rate: {b['over_budget_rate']:.3f} -> "
            f"{f['over_budget_rate']:.3f} "
            f"({improvement(b['over_budget_rate'], f['over_budget_rate'])})"
        )
        b_cost = 1 / b["good_success_per_usd"] if b["good_success_per_usd"] else float("inf")
        f_cost = 1 / f["good_success_per_usd"] if f["good_success_per_usd"] else float("inf")
        print(
            f"{VERIFY} slo_cost_per_success_usd: {b_cost:.6f} -> {f_cost:.6f} "
            f"({improvement(b_cost, f_cost)})"
        )
        print(
            f"{VERIFY} good_success_per_usd: {b['good_success_per_usd']:.1f} -> "
            f"{f['good_success_per_usd']:.1f} "
            f"({improvement(b['good_success_per_usd'], f['good_success_per_usd'], lower_is_better=False)})"
        )
        print(
            f"{VERIFY} cost_usd: {b['usd']:.5f} -> {f['usd']:.5f} "
            f"({improvement(b['usd'], f['usd'])})"
        )
        # 这里**故意不**对原始 success_rate 做断言：它在上游未恢复时会因为熔断
        # 主动拒绝而下降，方向随机抖动，用它下结论本身就是方法错误。
        # 稳定的口径是 slo_cost_per_success_usd（每美元买到多少 SLO 内成功）。

        head("4. 工程结论")
        note("1) 预算往下传：child_timeout = min(阶段上限, 父剩余预算)。")
        note("2) 重试挂同一个 deadline，并且有全局重试预算，防重试风暴放大 3^N 流量。")
        note("3) 熔断要统计慢调用：'不报错但很慢'比报错更能拖死自己。")
        note("4) 半开只放 1 个探针，恢复期不要放量。")
        note("5) 舱壁满 → 立刻失败并返回可重试信号，别让请求在内存里排队。")
        note("6) 熔断不是「纯粹的优化」：它会主动拒绝请求。必须和降级链/兜底答案配合使用，")
        note("   否则用户看到的是「更快地失败」而不是「更快地成功」。")
        note(f"7) 本轮 v1b 熔断器统计：{f['breaker']}")
        takeaway(
            "超时是预算不是常数；重试必须在预算内；熔断是用可用性换延迟与成本的止损开关，"
            "必须配降级链才完整。"
        )
        METRICS.reset()
    return 0


QUESTIONS = [
    "LLM 响应慢，导致整个 agent 链路超时，分层超时的熔断策略是什么？ "
    "-> Deadline 分层预算 + 熔断三态机 + 慢调用计入失败",
    "第三方 LLM 接口不稳定抖动，同时有超时和报错，如何保证 agent 系统可用？ "
    "-> 全抖动退避重试 + 共享重试预算 + 熔断 + 舱壁快速失败",
    "生产环境并发量一高 agent 就会挂 —— 部分根因是慢调用占满线程且无快速失败通道",
]


if __name__ == "__main__":
    sys.exit(main())
