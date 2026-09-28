"""Capstone 韧性层：把 lab-04 / lab-06 / lab-13 的结论装配成一套可用组件。

设计要点：
* 熔断、舱壁、限流都是**有状态**的，必须按维度实例化（模型 / 租户 / 工具），
  不能全局共用一个——共用一个就等于没有隔离。
* 每个组件都有独立的开关和降级路径，故障时"逐层退让"而不是整体崩掉。
* 所有拒绝都必须带**可重试信号**（retry_after），否则调用方只能盲目重试。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from agentlab.metrics import METRICS
from agentlab.orchestration import Bulkhead, CircuitBreaker, RetryBudget, RetryPolicy, TokenBucket
from agentlab.providers import LLMError
from agentlab.tokens import LADDER, MODELS, CostLedger, ModelSpec


# --------------------------------------------------------------------------
# 按维度管理熔断器 / 舱壁
# --------------------------------------------------------------------------


class BreakerRegistry:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()
        self.m_opened = METRICS.counter("platform_breaker_opened_total", "熔断打开总数")

    def get(self, key: str) -> CircuitBreaker:
        with self._lock:
            cb = self._breakers.get(key)
            if cb is None:
                cb = CircuitBreaker(
                    key,
                    failure_threshold=self.cfg.breaker_failure_threshold,
                    cooldown_s=self.cfg.breaker_cooldown_s,
                    half_open_max=self.cfg.breaker_half_open_max,
                    slow_call_ms=self.cfg.breaker_slow_call_ms,
                )
                self._breakers[key] = cb
            return cb

    def render(self) -> None:
        print("\n  ┌─ 熔断器状态")
        for k, cb in sorted(self._breakers.items()):
            print(f"  │ {cb.stats()}")
        print("  └" + "─" * 62)


class BulkheadRegistry:
    """全局 + 每租户双层舱壁。批处理请求被限制在全局容量的一部分。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.global_pool = Bulkhead("global", cfg.global_concurrency, wait_s=0.05)
        self.batch_pool = Bulkhead(
            "batch",
            max(1, int(cfg.global_concurrency * cfg.batch_max_ratio)),
            wait_s=0.02,
        )
        self._tenant: dict[str, Bulkhead] = {}
        self._lock = threading.Lock()

    def tenant_pool(self, tenant: str) -> Bulkhead:
        with self._lock:
            bh = self._tenant.get(tenant)
            if bh is None:
                if len(self._tenant) >= 64:
                    tenant = "__overflow__"
                    bh = self._tenant.get(tenant)
                if bh is None:
                    bh = Bulkhead(f"tenant_{tenant}", self.cfg.tenant_concurrency, wait_s=0.05)
                    self._tenant[tenant] = bh
            return bh

    def render(self) -> None:
        print("\n  ┌─ 舱壁状态（隔离 = 不能用同一个池子）")
        print(f"  │ {self.global_pool.stats()}")
        print(f"  │ {self.batch_pool.stats()}  <- 批处理最多占全局 {self.cfg.batch_max_ratio:.0%}")
        for t, bh in sorted(self._tenant.items()):
            print(f"  │ {bh.stats()}")
        print("  └" + "─" * 62)


# --------------------------------------------------------------------------
# 分层限流
# --------------------------------------------------------------------------


class RateLimiterStack:
    """四层限流：全局 → 租户 → 模型 → 工具。

    为什么要四层：每一层保护的对象不同。
    全局保护自己；租户层做公平；模型层保护上游配额；工具层保护下游依赖。
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.global_bucket = TokenBucket(
            cfg.global_concurrency * 6, cfg.global_concurrency * 10, name="global"
        )
        self._tenant: dict[str, TokenBucket] = {}
        self._model: dict[str, TokenBucket] = {}
        self._tool: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self.m_reject = METRICS.counter("platform_ratelimit_rejected_total", "限流拒绝总数")

    def _bucket(self, store: dict[str, TokenBucket], key: str, rate: float, burst: float) -> TokenBucket:
        with self._lock:
            b = store.get(key)
            if b is None:
                b = TokenBucket(rate, burst, name=key)
                store[key] = b
            return b

    def tenant_bucket(self, tenant: str) -> TokenBucket:
        return self._bucket(self._tenant, f"t:{tenant}", self.cfg.tenant_qps, self.cfg.tenant_burst)

    def model_bucket(self, model: str) -> TokenBucket:
        """客户端自限流：按**吞吐**（并发/延迟）而不是并发数来配置。

        留 headroom 的道理：上游一旦排队，排队时间算进你的超时预算，而且排队
        期间它那边的并发槽被你占着。主动限流 + 快速失败通常比打到上游 429 便宜。
        """
        spec = MODELS.get(model)
        if spec is None:
            return self._bucket(self._model, f"m:{model}", 8.0, 16.0)
        ratio = getattr(self.cfg, "model_headroom_ratio", 1.0)
        rate = max(1.0, spec.capacity_qps() * ratio)
        return self._bucket(self._model, f"m:{model}", rate, rate * 2)

    def tool_bucket(self, tool: str) -> TokenBucket:
        return self._bucket(self._tool, f"x:{tool}", 20.0, 40.0)

    def check(self, tenant: str, model: str, tool: str | None = None, wait_s: float = 0.0) -> None:
        """任何一层拒绝都立刻抛可重试错误，并带上 retry_after。

        ``wait_s`` 让调用方可以在自己的预算内"等一下再试"——这是把限流从
        "制造错误"变成"平滑削峰"的关键开关，但窗口必须很短（< 200ms），
        否则限流就退化成无界排队。
        """
        for bucket, label in (
            (self.global_bucket, "global"),
            (self.tenant_bucket(tenant), f"tenant:{tenant}"),
            (self.model_bucket(model), f"model:{model}"),
        ):
            if not bucket.acquire(wait_s=wait_s):
                self.m_reject.inc()
                raise LLMError.rate_limited(
                    bucket.retry_after_s(), f"{label} 限流（{bucket.name}）"
                )
        if tool:
            b = self.tool_bucket(tool)
            if not b.acquire(wait_s=wait_s):
                self.m_reject.inc()
                raise LLMError.rate_limited(b.retry_after_s(), f"tool:{tool} 限流")

    def render(self) -> None:
        print("\n  ┌─ 限流层状态")
        print(f"  │ {self.global_bucket.stats()}")
        for k, b in sorted(self._tenant.items()):
            print(f"  │ {b.stats()}")
        for k, b in sorted(self._model.items()):
            print(f"  │ {b.stats()}")
        print("  └" + "─" * 62)


# --------------------------------------------------------------------------
# 模型路由
# --------------------------------------------------------------------------


@dataclass
class RouteDecision:
    model: str
    reason: str
    escalated: bool = False
    downgraded: bool = False
    fallback_level: int = 0  # 0=正常, 1=mid, 2=small, 3=缓存/模板


class ModelRouter:
    """成本感知的级联路由 + 熔断降级链 + 租户预算约束。

    路由决策的三条硬规则（**全部硬编码，绝不能问模型自己该用哪个模型**）::

        1. 任务复杂度（长度/是否要工具/是否多跳）决定**起始档位**
        2. 自置信度低于阈值 → 升级到更大模型（用钱买准确率）
        3. 租户预算不足 / 大模型熔断 → 强制降级，并如实告知质量下降

    规则 3 是很多团队缺失的一环：预算烧完了还在无脑调大模型。
    """

    def __init__(self, cfg, breakers: BreakerRegistry, ledger: CostLedger) -> None:
        self.cfg = cfg
        self.breakers = breakers
        self.ledger = ledger
        self._spend: dict[str, float] = {}
        self._lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self.escalations = 0
        self.downgrades = 0
        self.fallbacks = 0
        self.m_escalate = METRICS.counter("router_escalated_total", "升级到大模型")
        self.m_downgrade = METRICS.counter("router_downgraded_total", "降级到小模型")
        self.m_fallback = METRICS.counter("router_fallback_total", "走兜底路径")

    # -- 复杂度评估 ---------------------------------------------------------
    @staticmethod
    def complexity(prompt_tokens: int, needs_tools: bool, multi_hop: bool) -> str:
        """复杂度分档。**阈值是容量规划的一部分**：分档越松，越贵的模型占比越高。

        生产上要拿真实流量回放来调这三个阈值，而不是凭感觉设。
        """
        score = 0
        if prompt_tokens > 4000:
            score += 2
        elif prompt_tokens > 1600:
            score += 1
        if needs_tools:
            score += 1
        if multi_hop:
            score += 2
        if score >= 3:
            return "hard"
        if score >= 1:
            return "medium"
        return "easy"

    # -- 预算 ---------------------------------------------------------------
    def spend_of(self, tenant: str) -> float:
        with self._lock:
            return self._spend.get(tenant, 0.0)

    def budget_left(self, tenant: str) -> float:
        return self.cfg.tenant_daily_budget_usd - self.spend_of(tenant)

    # -- 决策 ---------------------------------------------------------------
    def initial_route(self, tenant: str, prompt_tokens: int, needs_tools: bool, multi_hop: bool) -> RouteDecision:
        level = self.complexity(prompt_tokens, needs_tools, multi_hop)
        mode = self.cfg.routing_mode
        left = self.budget_left(tenant)

        if mode == "all_small":
            model, reason = "small-8b", "策略=全小模型"
        elif mode == "all_large":
            model, reason = "large-400b", "策略=全大模型"
        elif mode == "static":
            model = {"easy": "small-8b", "medium": "mid-32b", "hard": "large-400b"}[level]
            reason = f"静态规则路由（复杂度={level}）"
        else:  # cascade
            model = "small-8b" if level in ("easy", "medium") else "mid-32b"
            reason = f"级联路由起始档（复杂度={level}）"

        # 预算闸门：快烧完就降级（硬编码，不可协商）
        if left <= 0 and model != "small-8b":
            self.downgrades += 1
            self.m_downgrade.inc()
            model, reason = "small-8b", f"租户预算耗尽（剩余 ${left:.4f}）→ 强制降级"

        # 熔断闸门：目标模型不可用就沿降级链下移
        decision = RouteDecision(model=model, reason=reason)
        decision = self._apply_fallback_chain(decision)
        with self._lock:
            self.counts[decision.model] = self.counts.get(decision.model, 0) + 1
        return decision

    def _apply_fallback_chain(self, d: RouteDecision) -> RouteDecision:
        """降级链 large → mid → small → 兜底（缓存/模板）。"""
        chain = [m.name for m in reversed(LADDER)]  # large, mid, small
        try:
            idx = chain.index(d.model)
        except ValueError:
            return d
        while idx < len(chain):
            name = chain[idx]
            cb = self.breakers.get(f"llm:{name}")
            if cb.state == "closed":
                break
            idx += 1
        if idx >= len(chain):
            self.fallbacks += 1
            self.m_fallback.inc()
            return RouteDecision(
                model="small-8b",
                reason=f"全链路熔断 → 兜底（原目标 {d.model}）",
                downgraded=True,
                fallback_level=3,
            )
        if chain[idx] != d.model:
            self.fallbacks += 1
            self.m_fallback.inc()
            return RouteDecision(
                model=chain[idx],
                reason=f"{d.model} 熔断 → 降级到 {chain[idx]}",
                downgraded=True,
                fallback_level=idx,
            )
        return d

    def should_escalate(self, confidence: float, current: str) -> str | None:
        """自置信度不足则升级一档。返回 None 表示不升级。"""
        if current == "large-400b":
            return None
        if confidence >= self.cfg.cascade_upgrade_confidence:
            return None
        order = ["small-8b", "mid-32b", "large-400b"]
        nxt = order[min(len(order) - 1, order.index(current) + 1)]
        cb = self.breakers.get(f"llm:{nxt}")
        if cb.state == "open":
            return None
        self.escalations += 1
        self.m_escalate.inc()
        return nxt

    def record_cost(self, tenant: str, usd: float) -> None:
        with self._lock:
            self._spend[tenant] = self._spend.get(tenant, 0.0) + usd

    def render(self) -> None:
        print("\n  ┌─ 模型路由状态")
        print(f"  │ 策略={self.cfg.routing_mode} 调用分布={self.counts}")
        print(
            f"  │ 升级={self.escalations} 降级={self.downgrades} 兜底={self.fallbacks}"
        )
        print("  └" + "─" * 62)


# --------------------------------------------------------------------------
# 重试配置
# --------------------------------------------------------------------------


def build_retry_policy(cfg) -> RetryPolicy:
    return RetryPolicy(
        max_retries=cfg.max_retries,
        base_s=cfg.retry_base_s,
        cap_s=cfg.retry_cap_s,
        jitter="full",
    )


def build_retry_budget(cfg) -> RetryBudget:
    # 重试预算是**整个进程共享**的：单点失败不应该引发全网重试风暴
    return RetryBudget(max(32, cfg.global_concurrency * cfg.retry_budget_per_request))
