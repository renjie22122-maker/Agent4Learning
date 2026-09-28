"""Capstone 压测与 SLO 合规报告。

一次真实的多租户混合负载，然后回答 4 个问题：

1. **性能**：P50/P95/P99、超 SLO 比例、吞吐
2. **成本**：每次调用、每次"成功任务"的成本、按租户分摊
3. **稳定性**：成功率、错误码分布、熔断/降级/限流触发次数
4. **隔离**：跨会话/跨租户事故是否为 0；单租户打满是否影响其他租户

最后输出一份**合规判定**：每条 SLO 达标或超标，超标要给出下一步动作。
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field

from agentlab.metrics import METRICS
from agentlab.util import Stats, run_concurrently

from .context import RequestContext
from .engine import AgentEngine, AgentRequest, AgentResponse


@dataclass
class SLOReport:
    name: str
    target: str
    actual: str
    passed: bool
    action: str = ""

    def line(self) -> str:
        flag = "✅ PASS" if self.passed else "❌ FAIL"
        return f"  {flag}  {self.name:<26} 目标 {self.target:<16} 实际 {self.actual:<16} {self.action}"


#: 压测用的问句池。刻意混入"语义相近但答案不同"的问法，用来度量语义缓存的错误命中率。
QUERY_POOL: tuple[tuple[str, str], ...] = (
    ("缓存穿透怎么治理", "A"),
    ("缓存穿透的治理方案", "A"),
    ("如何解决缓存穿透", "A"),
    ("熔断策略怎么设计", "B"),
    ("熔断器应该怎么配置", "B"),
    ("限流算法用哪种", "C"),
    ("内存泄漏怎么排查", "D"),
    ("怎么定位内存泄漏", "D"),
    ("3 + 5 * 2 等于多少", "E"),
    ("多租户怎么做隔离", "F"),
    ("租户隔离的工程做法", "F"),
    ("token 成本怎么降", "G"),
)


@dataclass
class TenantProfile:
    tenant_id: str
    users: int = 8
    weight: float = 1.0
    batch_ratio: float = 0.2
    budget_usd: float = 2.0


class LoadGenerator:
    def __init__(
        self,
        cfg,
        engine: AgentEngine,
        platform,
        seed: int = 11,
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.platform = platform
        self.rng = random.Random(seed)
        self._lock = threading.Lock()

    def _make_request(self, tenant: TenantProfile, i: int) -> AgentRequest:
        query, truth = QUERY_POOL[self.rng.randrange(len(QUERY_POOL))]
        is_batch = self.rng.random() < (tenant.batch_ratio if tenant else 0.2)
        user = f"{tenant.tenant_id}-u{self.rng.randrange(tenant.users)}"
        ctx = RequestContext(
            tenant_id=tenant.tenant_id,
            user_id=user,
            # session_id 必须**全局唯一**（含 tenant+user），否则不同租户的
            # 会话会撞在一起 —— 这正是 lab-16 复现的串会话根因之一。
            session_id=f"{tenant.tenant_id}-{user}-s{self.rng.randrange(3)}",
            groups=frozenset({f"g{tenant.tenant_id[-1]}", "public"}),
            roles=frozenset({"viewer"}),
            is_batch=is_batch,
        )
        return AgentRequest(
            query=query,
            ctx=ctx,
            needs_tools="算" not in query,
            truth=truth,
        )

    def run(
        self,
        tenants: list[TenantProfile],
        requests_per_tenant: int,
        concurrency: int = 32,
    ) -> dict:
        """多租户混合负载。每个租户各发 ``requests_per_tenant`` 个请求。"""
        jobs: list[tuple[TenantProfile, int]] = []
        for t in tenants:
            n = int(requests_per_tenant * t.weight)
            jobs.extend((t, i) for i in range(n))
        self.rng.shuffle(jobs)

        results: list[AgentResponse | BaseException] = [None] * len(jobs)  # type: ignore[list-item]
        t0 = time.perf_counter()

        def work(idx: int):
            t, i = jobs[idx]
            req = self._make_request(t, i)
            # 结果列表按**提交下标**回填，避免因为异常导致结果与 job 错位
            try:
                results[idx] = self.platform.handle(req)
            except BaseException as exc:  # noqa: BLE001 - 服务不可用等
                results[idx] = exc
            return idx

        run_concurrently(work, len(jobs), concurrency)
        wall = time.perf_counter() - t0
        results = list(results)

        return self._analyze(results, jobs, tenants, wall)
    # -- 分析 ---------------------------------------------------------------
    def _analyze(self, results, jobs, tenants, wall_s: float) -> dict:
        latencies: list[float] = []
        ok = 0
        errs: dict[str, int] = {}
        degraded = 0
        cached = 0
        by_tenant: dict[str, dict] = {
            t.tenant_id: {"n": 0, "ok": 0, "lat": [], "usd": 0.0, "batch": 0, "interactive": 0,
                          "interactive_lat": []}
            for t in tenants
        }
        usd_total = 0.0
        tokens_in = tokens_out = 0
        cache_layers: dict[str, int] = {}

        for res, (t, _i) in zip(results, jobs):
            slot = by_tenant[t.tenant_id]
            slot["n"] += 1
            if isinstance(res, AgentResponse):
                latencies.append(res.latency_ms)
                slot["lat"].append(res.latency_ms)
                usd_total += res.usd
                slot["usd"] += res.usd
                tokens_in += res.tokens_in
                tokens_out += res.tokens_out
                if res.ok:
                    ok += 1
                    slot["ok"] += 1
                else:
                    key = res.error.split(":")[0] or "UNKNOWN"
                    errs[key] = errs.get(key, 0) + 1
                if res.degraded:
                    degraded += 1
                if res.cached:
                    cached += 1
                    cache_layers[res.cache_layer] = cache_layers.get(res.cache_layer, 0) + 1
            else:
                # 连服务都没进去（比如停机排空拒绝）：这类请求也要计入分母，
                # 否则成功率会被"只统计成功进入的请求"美化
                key = getattr(res, "code", None) or type(res).__name__
                errs[str(key)] = errs.get(str(key), 0) + 1

        n = max(1, len(results))
        st = Stats([x for x in latencies if x > 0])
        over = sum(1 for x in latencies if x > self.cfg.slo_p95_ms)
        cost_per_success = usd_total / ok if ok else float("inf")

        # 租户级公平性：各租户成功率
        rates = [slot["ok"] / max(1, slot["n"]) for slot in by_tenant.values()]
        fairness = (sum(rates) ** 2) / (len(rates) * sum(r * r for r in rates)) if rates and any(rates) else 0.0

        return {
            "wall_s": wall_s,
            "total": len(results),
            "ok": ok,
            "ok_rate": ok / n,
            "over_slo_rate": over / n,
            "stats": st,
            "errors": dict(sorted(errs.items(), key=lambda kv: -kv[1])),
            "degraded": degraded,
            "cached": cached,
            "cache_layers": cache_layers,
            "usd": usd_total,
            "cost_per_success": cost_per_success,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "qps": len(results) / wall_s if wall_s else 0.0,
            # **有效吞吐**：每秒"成功完成"的请求数。
            # 这是比成功率更公平的对比口径：如果两个版本成功率接近，但一个
            # 吞吐高 8 倍，那它的实际服务能力是 8 倍 —— 单看成功率会得出
            # "两边差不多"的错误结论。
            "goodput": ok / wall_s if wall_s else 0.0,
            "by_tenant": by_tenant,
            "fairness": fairness,
            "cache_stats": {
                "exact": self.engine.cache.exact.stats(),
                "semantic": self.engine.cache.semantic.stats(),
                "semantic_wrong": self.engine.cache.semantic.wrong_hits,
            },
            "isolation": {
                "session_hijack_blocked": self.engine.sessions.hijack_blocked,
                "cross_talk": self.engine.sessions.assert_no_cross_talk(),
            },
            "provider": self.engine.server.summary_lines(),
        }

    # -- 渲染 ---------------------------------------------------------------
    def render_report(self, r: dict, title: str = "SLO 合规报告") -> list[SLOReport]:
        cfg = self.cfg
        print("\n" + "=" * 78)
        print(f"  {title}")
        print("=" * 78)

        print("\n  ── 性能 ──")
        print(f"  {r['stats']}")
        print(
            f"  吞吐={r['qps']:.1f} req/s  有效吞吐={r['goodput']:.1f} 成功/s  "
            f"墙钟={r['wall_s']:.2f}s  超 SLO 比例={r['over_slo_rate']:.2%}"
        )

        print("\n  ── 成本 ──")
        print(
            f"  总花费=${r['usd']:.4f}  每请求=${r['usd'] / max(1, r['total']):.6f}  "
            f"每成功=${r['cost_per_success']:.6f}"
        )
        print(f"  tokens: in={r['tokens_in']} out={r['tokens_out']}")
        print("  按租户分摊:")
        for tid, slot in sorted(r["by_tenant"].items()):
            share = slot["usd"] / r["usd"] * 100 if r["usd"] else 0.0
            print(
                f"    {tid:<10} n={slot['n']:<4} 成功={slot['ok'] / max(1, slot['n']):.0%}  "
                f"${slot['usd']:.4f} ({share:.1f}%)"
            )

        print("\n  ── 稳定性 ──")
        print(f"  成功率={r['ok_rate']:.2%}  降级请求={r['degraded']}  缓存命中={r['cached']}")
        print(f"  错误分布={r['errors'] or '（无）'}")
        print(f"  缓存分层命中={r['cache_layers'] or '（无）'}")
        print(f"  {r['cache_stats']['semantic']}")

        print("\n  ── 隔离 ──")
        iso = r["isolation"]
        print(
            f"  跨会话事故={iso['cross_talk']}  会话劫持拦截={iso['session_hijack_blocked']}  "
            f"租户公平性指数={r['fairness']:.3f}"
        )

        print("\n  ── Provider 侧 ──")
        for line in r["provider"]:
            print(f"  {line}")

        # ---- SLO 判定 ----
        reports = [
            SLOReport(
                "P95 延迟",
                f"≤ {cfg.slo_p95_ms:.0f}ms",
                f"{r['stats'].p95:.0f}ms",
                r["stats"].p95 <= cfg.slo_p95_ms,
                "" if r["stats"].p95 <= cfg.slo_p95_ms else "→ 检查超时预算分配与检索耗时",
            ),
            SLOReport(
                "成功率（受配额限制）",
                f"≥ {cfg.slo_success_rate:.0%}",
                f"{r['ok_rate']:.1%}",
                r["ok_rate"] >= cfg.slo_success_rate,
                ""
                if r["ok_rate"] >= cfg.slo_success_rate
                else "→ 说明本次负载已超过上游配额：要么扩配额，要么降载/加缓存",
            ),
            SLOReport(
                "有效吞吐",
                f"≥ {cfg.slo_goodput_rps:.0f} 成功/s",
                f"{r['goodput']:.1f} 成功/s",
                r["goodput"] >= cfg.slo_goodput_rps,
                "" if r["goodput"] >= cfg.slo_goodput_rps else "→ 扩容上游配额 / 提高缓存命中率",
            ),
            SLOReport(
                "每成功成本",
                f"≤ ${cfg.slo_cost_per_success_usd:.4f}",
                f"${r['cost_per_success']:.5f}",
                r["cost_per_success"] <= cfg.slo_cost_per_success_usd,
                "" if r["cost_per_success"] <= cfg.slo_cost_per_success_usd else "→ 提高缓存命中率 / 增加小模型占比",
            ),
            SLOReport(
                "超预算请求占比",
                "≤ 1%",
                f"{r['over_slo_rate']:.2%}",
                r["over_slo_rate"] <= 0.01,
                "" if r["over_slo_rate"] <= 0.01 else "→ 收紧分层预算，增加快速失败",
            ),
            SLOReport(
                "跨会话 / 越权事故",
                "= 0",
                f"{iso['cross_talk']}",
                iso["cross_talk"] == 0,
                "" if iso["cross_talk"] == 0 else "→ 检查会话 key 是否含 (tenant,user,session)",
            ),
            SLOReport(
                "租户公平性",
                "≥ 0.90",
                f"{r['fairness']:.3f}",
                r["fairness"] >= 0.90,
                "" if r["fairness"] >= 0.90 else "→ 检查每租户限流与舱壁是否生效",
            ),
        ]
        print("\n  ── SLO 合规判定 ──")
        for rep in reports:
            print(rep.line())
        passed = sum(1 for x in reports if x.passed)
        print(f"\n  结论: {passed}/{len(reports)} 项达标")
        return reports


def build_engine(cfg, seed: int = 7, llm_cfg=None, server_override=None, guard=None):
    """工厂：把配置变成一个可压测的引擎 + 平台。

    ``llm_cfg`` 传入且配置为真实后端时，会走真实 LLM（``agentplat/llm.py``）；
    否则用内置模拟器。**两者对上层完全透明** —— 引擎、探针、缓存、熔断、
    成本归因都不需要知道自己在跟谁说话。

    ``guard`` 是成本护栏，只对真实后端生效（模拟器不花钱）。
    """
    from agentlab.providers import LLMServer
    from agentlab.store import BM25Index, build_corpus

    from .service import ServicePlatform

    docs = build_corpus(cfg.corpus_size, seed=seed)
    index = BM25Index(docs)

    server = server_override
    if server is None:
        use_real = False
        if llm_cfg is not None:
            from .llm import build_server

            server = build_server(llm_cfg, guard=guard)
            use_real = isinstance(server, LLMServer) and hasattr(server, "real_calls")
        else:
            server = LLMServer(max_queue=64, max_wait_s=2.0, seed=seed)
        if not use_real:
            # 只有模拟器才需要伪造延迟与错误率；真实后端由上游自己决定
            server.set_latency("small-8b", cfg.provider_p50_ms * 0.45)
            server.set_latency("mid-32b", cfg.provider_p50_ms)
            server.set_latency("large-400b", cfg.provider_p50_ms * 2.4)
            for name in ("small-8b", "mid-32b", "large-400b"):
                server.set_error_rate(name, cfg.provider_error_rate)

    from .tools import build_default_registry

    engine = AgentEngine(cfg, server, index)
    engine.tools = build_default_registry(cfg, index)
    platform = ServicePlatform(cfg, engine)
    return platform, engine
