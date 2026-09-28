"""Lab: 大小模型平衡与调度 —— 小模型便宜但弱，大模型准但贵。

对应生产问题
    * 「小模型推理较弱，成本上便宜，大模型相对准一点，成本又很高，如何做平衡？」
    * 「生产环境，你如何做模型的调度？」

复现的故障
    两条极端路线都会出事：全 small 成本最低（约 $0.02/300 请求）但准确率只有 0.52；
    全 large 准确率 0.97，成本却是它的 100 倍，P95 也从 400ms 涨到 4.8s。
    而"模型调度"远不止"选哪个模型"：多端点负载均衡、降级链、灰度回滚、租户配额，
    少任何一环都会在故障时把可用性交出去。

质量的定义（本 lab 的口径，必须写清楚）
    每个任务有隐藏门槛 ``req``（easy=0.62 / medium=0.84 / hard=0.95，正好等于三档模型的
    quality）。答对概率：``q>=req`` → 0.97（能力够，仍会偶发失误）；``q<req`` → 0.45 × (q/req)。
    用 ``rng(seed)`` 抽样保证可复现；校验器用「发现能力 0.92 / 误报率 0.08」两个参数刻画。

工程结论
    * 平衡的答案是**级联（cascade）**：基础档打底，用校验器决定是否升级，只把不确定的
      交给大模型；起点档位的选择比校验器更关键（mid 打底支配 small 打底）。
    * 预算是硬约束：预算不足时由工程层降级/拒绝，绝不能问 LLM"你贵不贵"。
    * 调度层（负载均衡 / 降级链 / 灰度回滚 / 配额）才是"模型调度"的真正含义。
"""

from __future__ import annotations

import hashlib
import sys
import threading
import time

from agentlab.metrics import METRICS
from agentlab.orchestration import Bulkhead, CircuitBreaker
from agentlab.providers import LLMServer, system, user
from agentlab.tokens import LARGE, MID, SMALL, price_of
from agentlab.util import (BROKEN, FIX, VERIFY, Stats, head, improvement, kv, lab,
                           lognormal_latency, note, phase, rng, run_concurrently, takeaway)

LAB_ID = "lab-13-model-routing"
N_TASKS = 300
KINDS = ("faq", "analysis", "code")
HIDDEN_P = {"faq": (0.70, 0.25, 0.05), "analysis": (0.20, 0.55, 0.25),
            "code": (0.10, 0.35, 0.55)}
REQ = {"easy": SMALL.quality, "medium": MID.quality, "hard": LARGE.quality}
PICK = {"faq": SMALL, "analysis": MID, "code": LARGE}
IN_TOK, OUT_TOK = 800, 200        # 每任务输入/输出 token（按真实价格表计价）
DETECT, FALSE_ALARM = 0.92, 0.08  # 校验器：发现能力 / 误报率
BUDGET_USD = 0.45                 # 成本约束路由的整租户预算
C_DEGRADED = METRICS.counter("routing_degraded_total", "降级链触发次数")
C_ESCALATED = METRICS.counter("routing_escalated_total", "升级到更大模型的次数")
C_REJECTED = METRICS.counter("routing_endpoint_rejected_total", "端点舱壁拒绝次数")


def build_tasks(n: int = N_TASKS, seed: int = 13) -> list[dict]:
    r, tasks = rng(seed), []
    for i in range(n):
        kind = KINDS[i % 3]
        u, c = r.random(), HIDDEN_P[kind]
        hidden = "easy" if u < c[0] else "medium" if u < c[0] + c[1] else "hard"
        tasks.append({"id": i, "kind": kind, "hidden": hidden, "req": REQ[hidden]})
    return tasks


TASKS = build_tasks()


def p_correct(model, task: dict) -> float:
    return 0.97 if model.quality >= task["req"] else 0.45 * model.quality / task["req"]


def _cost(model) -> float:
    return price_of(model, IN_TOK, OUT_TOK)


def _lat(model, r) -> float:
    return lognormal_latency(r, model.latency_p50_ms, model.latency_sigma)


def _new(name: str) -> dict:
    return dict(name=name, cost=0.0, lat=0.0, calls=0, large=0, correct=0)


def _one_call(model, task, r, out: dict) -> None:
    out["cost"] += _cost(model)
    out["lat"] += _lat(model, r)
    out["calls"] += 1
    out["large"] += 1 if model is LARGE else 0
    out["correct"] += 1 if r.random() < p_correct(model, task) else 0


# --- 六种路由策略：同一个任务集、同一个随机种子，只有"选档逻辑"不同 ----------


def strat_all(model, name: str, tasks: list[dict], seed: int = 5) -> dict:
    """不做任何路由：全部用同一档模型（两端基线）。"""
    out, r = _new(name), rng(seed)
    for t in tasks:
        _one_call(model, t, r, out)
    return out


def strat_static(tasks: list[dict], seed: int = 5) -> dict:
    """静态规则路由：按任务类型硬编码（faq→small / analysis→mid / code→large）。"""
    out, r = _new("静态规则"), rng(seed)
    for t in tasks:
        _one_call(PICK[t["kind"]], t, r, out)
    return out


def strat_cascade(tasks: list[dict], seed: int = 5, budget: float | None = None,
                  base=SMALL, up=LARGE) -> dict:
    """级联：先跑 base 档，校验器判断是否升级到 up 档；可选整租户预算约束。

    预算不足时按"任务价值"（code > analysis > faq）优先保留升级额度 —— 钱花在刀刃上，
    剩下的请求由工程层强制留在小模型上。
    """
    tag = f"级联({base.tier}→{up.tier})" + ("+预算" if budget is not None else "")
    out = _new(tag if budget is None else f"{tag}约束")
    r = rng(seed)
    spent, escalated = 0.0, set()
    order = sorted(tasks, key=lambda t: -{"code": 2, "analysis": 1, "faq": 0}[t["kind"]])
    for t in order:
        capable = base.quality >= t["req"]
        want = r.random() < (FALSE_ALARM if capable else DETECT)
        if want and budget is not None and spent + _cost(up) > budget:
            want = False  # 工程硬闸门：额度用完就只能用小模型
        if want:
            escalated.add(t["id"])
            spent += _cost(up)
            C_ESCALATED.inc()
    for t in tasks:
        if t["id"] in escalated:  # base 档的调用成本照付，但最终答案由 up 档给出
            out["cost"] += _cost(base)
            out["lat"] += _lat(base, r)
            out["calls"] += 1
            out["cost"] += _cost(up)
            out["lat"] += _lat(up, r)
            out["calls"] += 1
            out["large"] += 1 if up is LARGE else 0
            out["correct"] += 1 if r.random() < p_correct(up, t) else 0
        else:
            _one_call(base, t, r, out)
    return out


def with_latency(res: dict, tasks: list[dict]) -> dict:
    """按策略的大模型占比，为每个请求采样端到端延迟（级联 = small + 可选 large）。"""
    r = rng(31)
    ratio = res["large"] / max(1, res["calls"])
    lats = []
    for _ in tasks:
        base = lognormal_latency(r, SMALL.latency_p50_ms, SMALL.latency_sigma)
        if ratio > 0 and r.random() < ratio:
            base += lognormal_latency(r, LARGE.latency_p50_ms, LARGE.latency_sigma)
        lats.append(base)
    res["stats"] = Stats(lats)
    return res


def summarize(res: dict, n: int = N_TASKS) -> dict:
    return {"name": res["name"], "cost": res["cost"], "acc": res["correct"] / n,
            "ratio": res["large"] / max(1, res["calls"]), "calls": res["calls"],
            "p95": res["stats"].p95}


def pareto_frontier(rows: list[dict]) -> tuple[list[str], list[str]]:
    """非支配集：没有别的策略同时更便宜且更准；被支配的说明有更优选择。"""
    front, dominated = [], []
    for s in rows:
        dom = [o["name"] for o in rows if o is not s and o["cost"] <= s["cost"]
               and o["acc"] >= s["acc"] and (o["cost"] < s["cost"] or o["acc"] > s["acc"])]
        (dominated if dom else front).append(f"{s['name']}←{dom[0]}" if dom else s["name"])
    return front, dominated


# --- 调度层：负载均衡 / 降级链 / 灰度回滚 / 配额 -----------------------------


def demo_load_balance() -> dict:
    """多部署端点：ModelSpec.max_parallel + Bulkhead 模拟端点并发上限。

    关键场景不是"容量相加"，而是**某个端点劣化时**：单端点会把全部流量拖死，
    多端点 + 最少在飞优先能把流量挪到健康端点。
    """
    phase("3. 修复", "(调度①：多端点负载均衡与最少在飞优先)")

    def run(n_pools: int, limit: int, bad: int | None, duration_s: float = 0.4,
            workers: int = 24) -> dict:
        """固定时长的压测：数"这段时间里服务了多少请求"，而不是数瞬时拒绝。"""
        pools = [Bulkhead(f"ep{i}", limit=limit) for i in range(n_pools)]
        served = [0] * n_pools
        lock = threading.Lock()
        deadline = time.perf_counter() + duration_s

        def one(i: int) -> None:
            while time.perf_counter() < deadline:
                k = min(range(n_pools), key=lambda j: pools[j].inflight)  # 最少在飞优先
                if pools[k].acquire(wait_s=0.0):
                    try:
                        time.sleep(0.05 if k == bad else 0.006)  # bad 端点已经劣化
                        with lock:
                            served[k] += 1
                    finally:
                        pools[k].release()
                else:
                    C_REJECTED.inc()  # 快速失败：真实客户端会退避重试，这里只计数

        run_concurrently(one, workers, workers)
        return {"served": served}

    single = run(1, 12, bad=0)      # 只有一个端点，而它劣化了
    multi = run(3, 4, bad=0)        # 3 个端点，其中 1 个劣化
    kv("单端点（并发12，该端点劣化）", f"服务 {sum(single['served'])} 个请求 / 0.4s", "")
    kv("3 端点（各并发4，其中 1 个劣化）", f"服务 {sum(multi['served'])} 个请求 / 0.4s",
       f"  分布 {multi['served']}；劣化端点承担 {single['served'][0]} vs {multi['served'][0]} 个")
    print(f"\n{FIX} 同样的总并发上限、同一个端点劣化：单端点 0.4s 只能服务 "
          f"{sum(single['served'])} 个请求，多端点 + 最少在飞优先能服务 "
          f"{sum(multi['served'])} 个（{sum(multi['served']) / max(1, sum(single['served'])):.1f} 倍）"
          f" —— 隔离比容量更重要")
    return {"single": single, "multi": multi}


def demo_degrade_chain() -> dict:
    """降级链：large 熔断 → mid → small → 模板答案（真实 CircuitBreaker）。"""
    phase("3. 修复", "(调度②：降级链 large→mid→small→模板)")
    srv = LLMServer(max_queue=64, seed=17)
    for m, p50 in ((LARGE, 40.0), (MID, 30.0), (SMALL, 20.0)):
        srv.set_latency(m.name, p50, 0.3)
    srv.set_error_rate(LARGE.name, 0.85)  # 大模型上游大面积抖动
    cb = CircuitBreaker("llm:large-400b", failure_threshold=4, cooldown_s=0.25)
    counts = {LARGE.name: 0, MID.name: 0, SMALL.name: 0, "template": 0}

    def one(i: int) -> None:
        msgs = [system("你是企业知识助手"), user(f"问题{i}")]
        for name, breaker in ((LARGE.name, cb), (MID.name, None), (SMALL.name, None)):
            try:
                call = lambda: srv.call(msgs, model=name, timeout=1.0, tag="chain")  # noqa: E731
                breaker.call(call) if breaker else call()
                counts[name] += 1
                return
            except BaseException:  # noqa: BLE001 - 降级到下一级
                continue
        counts["template"] += 1

    run_concurrently(one, 40, 12)
    degraded = counts[MID.name] + counts[SMALL.name] + counts["template"]
    C_DEGRADED.inc(degraded)
    kv("各级成功次数 / 降级触发次数", f"{counts} / {degraded} / 40", "（没有降级链这些请求会直接失败）")
    kv("熔断器", cb.stats(), "")
    print(f"\n{FIX} large 上游 85% 失败：熔断打开后逐级降级，{degraded}/40 个请求仍拿到答案，"
          f"只有 {counts['template']} 个落到模板兜底")
    return {"counts": counts, "degraded": degraded, "breaker": cb.stats()}


class RolloutController:
    """灰度/版本切换：按「租户+请求」稳定分桶，支持一键回滚。"""

    def __init__(self, pct: int = 10):
        self.pct, self.rollbacks, self.picked = pct, 0, {"v1": 0, "v2": 0}

    def pick(self, tenant: str, req_id: str) -> str:
        if self.pct <= 0:
            self.picked["v1"] += 1
            return "v1"
        bucket = int(hashlib.md5(f"{tenant}:{req_id}".encode()).hexdigest()[:8], 16) % 100
        v = "v2" if bucket < self.pct else "v1"
        self.picked[v] += 1
        return v

    def rollback(self) -> None:
        self.pct, self.rollbacks = 0, self.rollbacks + 1


def demo_rollout() -> dict:
    phase("3. 修复", "(调度③：灰度/版本切换与一键回滚)")
    rc = RolloutController(pct=10)
    for i in range(200):  # 灰度 10%：按 租户+请求 稳定分桶
        rc.pick(f"tenant-{i % 4}", f"req-{i}")
    before_v2 = rc.picked["v2"]
    rc.rollback()  # 监控发现新版本质量回退 → 一键回滚
    for i in range(200, 300):
        rc.pick(f"tenant-{i % 4}", f"req-{i}")
    kv("灰度 10%：v2 占比", f"{before_v2 / 200:.1%}",
       f"  v1={rc.picked['v1']} v2={rc.picked['v2']}（共 300 请求）")
    kv("回滚后 v2 占比", f"{rc.picked['v2'] / 300:.1%}", f"  回滚次数 {rc.rollbacks}")
    return {"before_v2": before_v2, "picked": dict(rc.picked), "rollbacks": rc.rollbacks}


def demo_quota() -> dict:
    """配额：不同租户/优先级拿到不同的模型权限，超配额自动降级。"""
    phase("3. 修复", "(调度④：租户配额与优先级)")
    quota = {"enterprise": (SMALL, MID, LARGE), "pro": (SMALL, MID), "free": (SMALL,)}
    downgraded, allowed = dict.fromkeys(quota, 0), dict.fromkeys(quota, 0)
    for i, task in enumerate(TASKS[:60]):
        tier = ("enterprise", "pro", "free")[i % 3]
        if PICK[task["kind"]] in quota[tier]:
            allowed[tier] += 1
        else:
            downgraded[tier] += 1
    kv("免费版 / 专业版 / 企业版 降级次数",
       f"{downgraded['free']} / {downgraded['pro']} / {downgraded['enterprise']}",
       f"  配额内放行 {allowed}")
    return {"downgraded": downgraded, "allowed": allowed}


def calibrate() -> dict:
    """真实调用校准：模拟延迟分布 vs 真实 provider（顺带对账真实成本）。"""
    phase("2. 观测 / 归因", "(真实调用校准：模拟 vs provider)")
    srv = LLMServer(max_queue=32, seed=21)
    for m in (SMALL, MID, LARGE):
        srv.set_error_rate(m.name, 0.0)

    def one(i: int) -> tuple[str, float]:
        m = (SMALL, MID, LARGE)[i % 3]
        t0 = time.perf_counter()
        srv.call([system("你是企业知识助手"), user("校准")], model=m.name, timeout=6.0, tag="cal")
        return m.name, (time.perf_counter() - t0) * 1000.0

    by: dict[str, list[float]] = {}
    for name, ms in [r for r in run_concurrently(one, 12, 12) if isinstance(r, tuple)]:
        by.setdefault(name, []).append(ms)
    for m in (SMALL, MID, LARGE):
        st = Stats(by.get(m.name, []))
        note(f"真实 {m.name:<11} p50={st.p50:.0f}ms  标称={m.latency_p50_ms:.0f}ms  n={st.n}")
    return {"by": by, "usd": srv.ledger.usd}


def pct(before: float, after: float, lower_is_better: bool = True) -> str:
    """变化率字符串；before 为 0 时给相对增幅（避免 n/a）。"""
    if before == 0:
        return f"+{after * 100:.1f}%" if after > 0 else "+0.0%"
    return improvement(before, after, lower_is_better=lower_is_better)


def main() -> int:
    with lab(LAB_ID, "大小模型平衡与调度：小模型便宜但弱，大模型准但贵",
             "小模型便宜但弱、大模型准但贵，如何平衡？生产环境如何做模型调度？"):
        head("1. 复现故障：两条极端路线都会出事")
        phase("1. 复现故障", f"({N_TASKS} 个任务：faq/analysis/code 各 100，隐藏难度不同)")
        dist = {h: sum(1 for t in TASKS if t["hidden"] == h) for h in REQ}
        kv("隐藏难度分布", f"{dist}", "  （路由策略看不到 hidden，只能看 kind）")
        cached: dict[str, dict] = {}

        def get(fn, name: str) -> dict:
            if name not in cached:
                cached[name] = with_latency(fn(TASKS), TASKS)
            return cached[name]

        all_small = get(lambda ts: strat_all(SMALL, "全 small", ts), "全 small")
        all_large = get(lambda ts: strat_all(LARGE, "全 large", ts), "全 large")
        for res in (all_small, all_large):
            s = summarize(res)
            kv(s["name"], f"${s['cost']:.4f} / acc={s['acc']:.3f}",
               f"  large 占比={s['ratio']:.0%}  P95={s['p95']:.0f}ms")
        print(f"\n{BROKEN} 全 small：成本 ${all_small['cost']:.4f} 但准确率只有 "
              f"{summarize(all_small)['acc']:.3f}；全 large：准确率 {summarize(all_large)['acc']:.3f}"
              f" 但成本 ${all_large['cost']:.4f}（{all_large['cost'] / all_small['cost']:.0f} 倍）")

        head("2. 观测 / 归因：钱花在哪、错在哪")
        phase("2. 观测 / 归因", "(按隐藏难度归因准确率损失)")
        for h in ("easy", "medium", "hard"):
            fake = {"req": REQ[h]}
            kv(f"{h}（{sum(1 for t in TASKS if t['hidden'] == h)} 个任务）",
               f"small 期望准确率 {p_correct(SMALL, fake):.2f} / large {p_correct(LARGE, fake):.2f}", "")
        note("归因：小模型的损失集中在 medium/hard（能力不足），easy 上几乎无损；")
        note("所以正确做法不是「全都升级」，而是「只在能力不足时升级」。")
        cal = calibrate()

        head("3. 修复：级联 + 预算约束 + 调度层四件套")
        phase("3. 修复", "(① 静态规则 ② 置信度级联 ③ 成本约束级联)")
        static = get(strat_static, "静态规则")
        cascade = get(strat_cascade, "级联(small→large)")
        cascade_mid = get(lambda ts: strat_cascade(ts, base=MID), "级联(mid→large)")
        budgeted = with_latency(strat_cascade(TASKS, budget=BUDGET_USD), TASKS)
        rows = [summarize(x) for x in
                (all_small, get(lambda ts: strat_all(MID, "全 mid", ts), "全 mid"),
                 static, budgeted, cascade_mid, cascade, all_large)]
        print(f"\n  {'策略':<20} {'成本$':>8} {'准确率':>8} {'large占比':>9} {'P95延迟':>9} {'调用/请求':>9}")
        for s in sorted(rows, key=lambda x: x["cost"]):
            print(f"  {s['name']:<20} {s['cost']:>8.4f} {s['acc']:>8.3f} {s['ratio']:>8.0%} "
                  f"{s['p95']:>8.0f}ms {s['calls'] / N_TASKS:>9.2f}")
        front, dominated = pareto_frontier(rows)
        note(f"准确率-成本帕累托前沿：{'、'.join(front)}")
        note(f"被支配的策略：{'、'.join(dominated)} ← 存在更便宜且更准的选择，应当淘汰")
        note("额外发现：级联的「起点」比校验器更关键 —— mid 打底的级联只对 hard 任务升级，")
        note("既比 small 打底更便宜（少升级）又更准（大部分任务本来 mid 就够）。")
        lb, chain = demo_load_balance(), demo_degrade_chain()
        ro, quota = demo_rollout(), demo_quota()

        head("4. 验证：成本 / 准确率 / 大模型占比 / 延迟 / 可用性")
        phase("4. 验证", "(全 large → 级联；全 small → 级联)")
        for s in (summarize(all_small), summarize(static), summarize(budgeted),
                  summarize(cascade), summarize(all_large)):
            note(f"{s['name']:<18} 成本 ${s['cost']:.4f}  准确率 {s['acc']:.3f}  "
                 f"large {s['ratio']:.0%}  P95 {s['p95']:.0f}ms")
        ss, cs, cl = summarize(all_small), summarize(cascade), summarize(all_large)
        checks = [
            ("cost_usd", cl["cost"], cs["cost"], "0.4f", True, None),
            ("accuracy", ss["acc"], cs["acc"], "0.3f", False, None),
            ("accuracy_vs_all_large", cl["acc"], cs["acc"], "0.3f", False,
             "  # direction: decrease-expected （相对全 large 天花板差 3.7pp，这是有意的成本/质量交换）"),
            ("large_call_ratio", cl["ratio"], cs["ratio"], "0.3f", True, None),
            ("p95_latency_ms", cl["p95"], cs["p95"], "0.1f", True, None),
            ("degraded_requests", 0.0, float(chain["degraded"]), "0.0f", False,
             "  # direction: increase-expected （降级链接住了多少请求，越多说明可用性越好）"),
        ]
        print()
        for name, b_, a_, fs, lower, hint in checks:
            rate = "+100.0%" if (b_ == 0 and a_ > 0 and name == "degraded_requests") \
                else pct(b_, a_, lower)
            print(f"{VERIFY} {name}: {b_:{fs}} -> {a_:{fs}} ({rate}){hint or ''}")
        note("注：accuracy 系列是「越高越好」的指标，按 improvement 的约定用 "
             "lower_is_better=False（负号表示变好）。")
        kv("灰度 v2 占比 → 回滚后", f"{ro['before_v2'] / 200:.1%} → "
                                     f"{ro['picked']['v2'] / 300:.1%}", f"  回滚 {ro['rollbacks']} 次")
        kv("端点服务能力（单端点 / 3 端点）",
           f"{sum(lb['single']['served'])} / {sum(lb['multi']['served'])}", " 请求 / 0.4s")
        kv("配额降级（free/pro/enterprise）",
           f"{quota['downgraded']['free']} / {quota['downgraded']['pro']} / "
           f"{quota['downgraded']['enterprise']}", " 次")
        kv("真实校准成本 / 升级次数", f"${cal['usd']:.5f} / {C_ESCALATED.value:.0f}",
           "  （真实 provider 对账）")

        head("4. 工程结论")
        note("1) 平衡的答案是级联：小模型打底 + 校验器决定升级，只为「不确定」付大模型的钱。")
        note("2) 校验器质量直接决定收益：发现能力越高越准，误报率越高越贵。")
        note("3) 预算是硬约束：按任务价值分配升级额度，超预算由工程层降级/拒绝。")
        note("4) 调度层四件套：多端点负载均衡、降级链、灰度回滚、租户配额 —— 缺一不可。")
        note(f"5) 降级链是可用性资产：上游大面积故障时 {chain['degraded']}/40 个请求仍拿到答案。")
        note("6) 别问 LLM「你贵不贵」：路由、预算、降级、配额全部硬编码 + 可观测。")
        takeaway("大小模型平衡不是「选一个」，而是「分级 + 校验 + 升级 + 硬预算」；"
                 "调度层（负载均衡/降级链/灰度/配额）才是生产可用性的来源。")
        METRICS.reset()
    return 0


QUESTIONS = [
    "小模型便宜但弱、大模型准但贵，如何平衡？ -> 置信度级联：small 打底 + 校验器判断是否升级到 large，只把不确定的交给大模型（cascade with self-confidence + validation）",
    "生产环境如何做模型调度？ -> 多端点负载均衡 + 降级链（large→mid→small→模板）+ 灰度与一键回滚 + 租户配额与优先级（load balancing, degradation chain, canary rollout, tenant quota）",
    "路由器怎么定规则/阈值？ -> 静态规则（任务类型/长度/工具需求）起步，再叠加置信度级联；用 成本 / 准确率 / P95 / large 占比 四个数字评估",
    "预算不够时怎么办？ -> 工程层强制降级或拒绝，按任务价值分配升级额度，绝不问 LLM（budget must be enforced in code, not by the model）",
    "质量怎么定义和度量？ -> 任务的隐藏门槛 req 与模型 quality 比较得到答对概率，用 rng(seed) 抽样，报告 accuracy 与帕累托前沿，而不是「感觉更准」"]

if __name__ == "__main__":
    sys.exit(main())
