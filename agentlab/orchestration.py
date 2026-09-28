"""编排层：分层超时预算 / 熔断 / 舱壁隔离 / 重试 / 限流。

这一层是"生产级 agent"和"demo agent"的分界线。四个原语各自解决的问题：

============  ==========================================================
Deadline      一次请求的总预算，向下传递。任何一层都不能超支，
              否则会出现"上游等 10s，下游还在为它干活"的雪崩。
CircuitBreaker 上游持续失败时**主动停止调用**。注意它保护的是
              *你自己*：不熔断 → 线程/连接全被慢调用占满。
Bulkhead      舱壁隔离。按租户/模型/工具分池，一个租户打满不影响别人。
RetryPolicy   带抖动的指数退避 + 重试预算上限（防止重试风暴放大流量）。
TokenBucket   限流。稳定速率 + 有限突发，多租户公平。
============  ==========================================================
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from .clock import Clock, RealClock
from .metrics import METRICS
from .providers import BudgetExceeded, CircuitOpen, LLMError

T = TypeVar("T")


# --------------------------------------------------------------------------
# 分层超时预算
# --------------------------------------------------------------------------


@dataclass
class Deadline:
    """一次请求的时间预算，向下分层传递。

    用法::

        dl = Deadline.root(6000, stages={"retrieve": 0.15, "llm": 0.5, "tools": 0.25})
        with dl.stage("retrieve") as s:
            if s.remaining_ms() <= 0: raise BudgetExceeded(...)

    生产要铁律：**子阶段预算之和 <= 父预算**，并且每个下游调用都必须用
    ``min(剩余预算, 该阶段上限)`` 作为自己的 timeout。很多"链路超时"事故
    就是某一层用了硬编码 30s 超时，把整条链路拖死。
    """

    total_ms: float
    started: float
    clock: Clock
    stages: dict[str, float] = field(default_factory=dict)
    used: dict[str, float] = field(default_factory=dict)
    name: str = "request"

    @classmethod
    def root(
        cls,
        total_ms: float,
        stages: dict[str, float] | None = None,
        clock: Clock | None = None,
        name: str = "request",
    ) -> "Deadline":
        c = clock or RealClock()
        return cls(total_ms, c.monotonic(), c, dict(stages or {}), {}, name)

    # -- 时间 ---------------------------------------------------------------
    def elapsed_ms(self) -> float:
        return (self.clock.monotonic() - self.started) * 1000.0

    def remaining_ms(self) -> float:
        return self.total_ms - self.elapsed_ms()

    def expired(self) -> bool:
        return self.remaining_ms() <= 0

    def fraction_used(self) -> float:
        return min(1.0, self.elapsed_ms() / max(self.total_ms, 1e-9))

    # -- 分层 ---------------------------------------------------------------
    def stage_budget_ms(self, stage: str) -> float:
        """该阶段的预算 = min(阶段上限, 当前剩余)。"""
        cap = self.stages.get(stage, self.remaining_ms())
        return max(0.0, min(cap, self.remaining_ms()))

    def stage(self, stage: str) -> "_StageScope":
        return _StageScope(self, stage)

    def timeout_s(self, stage: str, floor_s: float = 0.0) -> float:
        """给下游调用用的 timeout（秒）。返回 0 表示已经没有预算了。"""
        ms = self.stage_budget_ms(stage)
        if ms <= 0 and floor_s <= 0:
            return 0.0
        return max(floor_s, ms / 1000.0)

    def snapshot(self) -> dict[str, float]:
        return {
            "total_ms": self.total_ms,
            "elapsed_ms": self.elapsed_ms(),
            "remaining_ms": self.remaining_ms(),
            **{f"stage.{k}": v for k, v in self.used.items()},
        }

    def render(self, title: str = "超时预算") -> None:
        print(f"\n  ┌─ {title}（{self.name}）")
        bar_total = 40
        for stage, cap in self.stages.items():
            used = self.used.get(stage, 0.0)
            filled = int(min(1.0, used / max(cap, 1e-9)) * bar_total)
            over = "  ⚠超支" if used > cap else ""
            print(
                f"  │ {stage:<12} [{('█' * filled).ljust(bar_total)}] "
                f"{used:7.1f}/{cap:7.1f}ms{over}"
            )
        print(
            f"  │ {'TOTAL':<12} elapsed={self.elapsed_ms():.1f}ms "
            f"remaining={self.remaining_ms():.1f}ms/{self.total_ms:.0f}ms"
        )
        print("  └" + "─" * 60)


class _StageScope:
    """``with dl.stage("llm") as s:`` —— 自动记录该阶段实际耗时。"""

    def __init__(self, deadline: Deadline, stage: str):
        self.dl = deadline
        self.stage = stage
        self.enter_at = 0.0
        self.budget_ms = 0.0

    def __enter__(self) -> "_StageScope":
        self.budget_ms = self.dl.stage_budget_ms(self.stage)
        if self.budget_ms <= 0:
            raise BudgetExceeded(self.stage, self.dl.total_ms)
        self.enter_at = self.dl.clock.monotonic()
        return self

    @property
    def remaining_ms(self) -> float:
        return self.budget_ms - (self.dl.clock.monotonic() - self.enter_at) * 1000.0

    def timeout_s(self, floor_s: float = 0.0) -> float:
        return max(floor_s, self.remaining_ms / 1000.0)

    def check(self) -> None:
        if self.remaining_ms <= 0:
            raise BudgetExceeded(self.stage, self.budget_ms)

    def __exit__(self, exc_type, exc, tb) -> bool:
        spent = (self.dl.clock.monotonic() - self.enter_at) * 1000.0
        self.dl.used[self.stage] = self.dl.used.get(self.stage, 0.0) + spent
        return False


# --------------------------------------------------------------------------
# 熔断器
# --------------------------------------------------------------------------


@dataclass
class BreakerState:
    state: str = "closed"  # closed | open | half_open
    failures: int = 0
    successes: int = 0
    opened_at: float = 0.0
    opened_count: int = 0
    rejected: int = 0
    half_open_inflight: int = 0


class CircuitBreaker:
    """三态熔断器（closed → open → half_open）。

    * ``failure_threshold``：连续失败多少次打开
    * ``cooldown_s``：打开后多久允许试探
    * ``half_open_max``：半开时允许几个试探请求（**只放一个**，
      否则恢复瞬间又是一波流量把上游再打死）
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        cooldown_s: float = 2.0,
        half_open_max: int = 1,
        clock: Clock | None = None,
        slow_call_ms: float | None = None,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.half_open_max = half_open_max
        self.clock = clock or RealClock()
        self.slow_call_ms = slow_call_ms
        self.st = BreakerState()
        self._lock = threading.Lock()
        self.m_open = METRICS.counter(f"breaker_{name}_opened_total", "熔断打开次数")
        self.m_reject = METRICS.counter(f"breaker_{name}_rejected_total", "熔断拒绝次数")

    # -- 状态机 -------------------------------------------------------------
    def allow(self) -> bool:
        with self._lock:
            if self.st.state == "open":
                if self.clock.monotonic() - self.st.opened_at >= self.cooldown_s:
                    self.st.state = "half_open"
                    self.st.half_open_inflight = 0
                else:
                    self.st.rejected += 1
                    self.m_reject.inc()
                    return False
            if self.st.state == "half_open":
                if self.st.half_open_inflight >= self.half_open_max:
                    self.st.rejected += 1
                    self.m_reject.inc()
                    return False
                self.st.half_open_inflight += 1
            return True

    def on_success(self, latency_ms: float = 0.0) -> None:
        with self._lock:
            if self.st.state == "half_open":
                self.st.state = "closed"
                self.st.failures = 0
                self.st.successes += 1
                self.st.half_open_inflight = max(0, self.st.half_open_inflight - 1)
                return
            self.st.failures = 0
            self.st.successes += 1
            if (
                self.slow_call_ms
                and latency_ms > self.slow_call_ms
                and self.st.state == "closed"
            ):
                # 慢调用也计入失败倾向（避免"不报错但很慢"拖死自己）
                self.st.failures += 1
                if self.st.failures >= self.failure_threshold:
                    self._open_locked()

    def on_failure(self, retryable: bool = True) -> None:
        with self._lock:
            if self.st.state == "half_open":
                self.st.state = "open"
                self._open_locked()
                return
            self.st.failures += 1
            if retryable and self.st.failures >= self.failure_threshold:
                self._open_locked()

    def _open_locked(self) -> None:
        self.st.state = "open"
        self.st.opened_at = self.clock.monotonic()
        self.st.opened_count += 1
        self.st.half_open_inflight = 0
        self.m_open.inc()

    def force_open(self) -> None:
        with self._lock:
            self._open_locked()

    def force_close(self) -> None:
        with self._lock:
            self.st = BreakerState()

    @property
    def state(self) -> str:
        return self.st.state

    def remaining_cooldown_s(self) -> float:
        if self.st.state != "open":
            return 0.0
        return max(0.0, self.cooldown_s - (self.clock.monotonic() - self.st.opened_at))

    def call(self, fn: Callable[[], T]) -> T:
        """包一层：不允许时抛 ``CircuitOpen``。"""
        if not self.allow():
            raise CircuitOpen(self.name, self.remaining_cooldown_s())
        t0 = time.perf_counter()
        try:
            out = fn()
        except BaseException as exc:  # noqa: BLE001
            retryable = getattr(exc, "retryable", False)
            self.on_failure(retryable)
            raise
        self.on_success((time.perf_counter() - t0) * 1000.0)
        return out

    def stats(self) -> str:
        return (
            f"{self.name}: state={self.st.state} failures={self.st.failures} "
            f"opened={self.st.opened_count} rejected={self.st.rejected}"
        )


# --------------------------------------------------------------------------
# 舱壁隔离
# --------------------------------------------------------------------------


class Bulkhead:
    """有界并发池。满了直接拒绝（快速失败）而不是无限排队。

    "高并发下 agent panic / 内存打满"的常见根因就是**无界排队**：
    请求堆在内存里，每个都带着 prompt 和中间结果。
    """

    def __init__(self, name: str, limit: int, wait_s: float = 0.0):
        self.name = name
        self.limit = limit
        self.wait_s = wait_s
        self._free = limit
        self._cond = threading.Condition()
        self.rejected = 0
        self.max_used = 0
        self.m_inflight = METRICS.gauge(f"bulkhead_{name}_inflight", "舱壁占用")
        self.m_reject = METRICS.counter(f"bulkhead_{name}_rejected_total", "舱壁拒绝")

    def acquire(self, wait_s: float | None = None) -> bool:
        budget = self.wait_s if wait_s is None else wait_s
        deadline = time.perf_counter() + budget
        with self._cond:
            while self._free <= 0:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    self.rejected += 1
                    self.m_reject.inc()
                    return False
                self._cond.wait(min(remaining, 0.05))
            self._free -= 1
            used = self.limit - self._free
            self.max_used = max(self.max_used, used)
            self.m_inflight.set(used)
            return True

    def release(self) -> None:
        with self._cond:
            self._free = min(self.limit, self._free + 1)
            self.m_inflight.set(self.limit - self._free)
            self._cond.notify()

    def __enter__(self) -> "Bulkhead":
        if not self.acquire():
            raise LLMError.rate_limited(0.1, f"bulkhead {self.name} 满（limit={self.limit}）")
        return self

    def __exit__(self, *exc) -> bool:
        self.release()
        return False

    def call(self, fn: Callable[[], T], wait_s: float | None = None) -> T:
        if not self.acquire(wait_s):
            raise LLMError.rate_limited(0.1, f"bulkhead {self.name} 满")
        try:
            return fn()
        finally:
            self.release()

    @property
    def inflight(self) -> int:
        return self.limit - self._free

    def stats(self) -> str:
        return (
            f"{self.name}: inflight={self.inflight}/{self.limit} "
            f"peak={self.max_used} rejected={self.rejected}"
        )


class BulkheadSet:
    """按 key（租户/模型/工具）分池，池子用满才开始拒绝。"""

    def __init__(self, prefix: str, limit_per_key: int, max_keys: int = 32):
        self.prefix = prefix
        self.limit_per_key = limit_per_key
        self.max_keys = max_keys
        self._pools: dict[str, Bulkhead] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Bulkhead:
        with self._lock:
            pool = self._pools.get(key)
            if pool is None:
                if len(self._pools) >= self.max_keys:
                    # 池子数量本身必须有界：否则"租户维度"的隔离会变成泄漏
                    key = "__overflow__"
                    pool = self._pools.get(key)
                if pool is None:
                    pool = Bulkhead(f"{self.prefix}_{key}", self.limit_per_key)
                    self._pools[key] = pool
            return pool

    def snapshot(self) -> dict[str, str]:
        return {k: v.stats() for k, v in self._pools.items()}


# --------------------------------------------------------------------------
# 重试
# --------------------------------------------------------------------------


@dataclass
class RetryPolicy:
    """带抖动的指数退避 + 重试预算。

    ``max_retries=0`` 表示不重试。``retry_budget`` 限制"整条链路上所有重试
    的总次数"，防止多层级各自重试造成流量爆炸（3 层 × 各重试 3 次 = 27 倍）。
    """

    max_retries: int = 2
    base_s: float = 0.08
    cap_s: float = 1.0
    jitter: str = "full"  # none | equal | full | decorrelated
    retry_on: tuple[str, ...] = ("429", "503", "TIMEOUT", "5xx")
    retry_budget: int = 20

    def backoff_s(self, attempt: int, rnd: random.Random | None = None) -> float:
        r = rnd or random
        raw = min(self.cap_s, self.base_s * (2 ** max(0, attempt - 1)))
        if self.jitter == "none":
            return raw
        if self.jitter == "equal":
            return raw / 2 + r.random() * raw / 2
        if self.jitter == "decorrelated":
            return min(self.cap_s, r.uniform(self.base_s, raw * 3))
        return r.random() * raw  # full jitter：抗重试风暴最有效

    def should_retry(self, exc: BaseException, attempt: int) -> bool:
        if attempt > self.max_retries:
            return False
        code = getattr(exc, "code", None)
        if code == "CIRCUIT_OPEN":
            return False  # 熔断打开时重试只会让情况更糟
        if code == "BUDGET":
            return False  # 预算已尽，重试没有意义
        retryable = getattr(exc, "retryable", False)
        if retryable:
            return True
        if code is None:
            return isinstance(exc, (TimeoutError, ConnectionError, OSError))
        return str(code) in self.retry_on


class RetryBudget:
    """共享的重试预算（整条链路一个）。用完就禁止继续重试。"""

    def __init__(self, total: int):
        self.total = total
        self._left = total
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self._left <= 0:
                return False
            self._left -= 1
            return True

    @property
    def left(self) -> int:
        return self._left


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    deadline: Deadline | None = None,
    budget: RetryBudget | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    rnd: random.Random | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """同步重试包装。任何一次尝试前都检查 deadline，避免"重试到天荒地老"。"""
    attempt = 0
    last: BaseException | None = None
    while True:
        attempt += 1
        if deadline is not None and deadline.expired():
            raise BudgetExceeded("retry", deadline.total_ms) from last
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001
            last = exc
            if not policy.should_retry(exc, attempt):
                raise
            if budget is not None and not budget.take():
                raise
            delay = policy.backoff_s(attempt, rnd)
            if deadline is not None:
                remaining = deadline.remaining_ms() / 1000.0
                if remaining <= delay:
                    raise BudgetExceeded("retry", deadline.total_ms) from exc
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)


# --------------------------------------------------------------------------
# 限流
# --------------------------------------------------------------------------


class TokenBucket:
    """令牌桶：稳定速率 ``rate`` 每秒，桶容量 ``burst``。

    ``acquire`` 默认不等待（快速失败），这是服务端限流的正确姿势；
    客户端要做的是退避重试或者降级，而不是原地阻塞。
    """

    def __init__(
        self,
        rate: float,
        burst: float | None = None,
        clock: Clock | None = None,
        name: str = "bucket",
    ):
        self.rate = rate
        self.burst = burst if burst is not None else rate
        self.clock = clock or RealClock()
        self.name = name
        self._tokens = self.burst
        self._last = self.clock.monotonic()
        self._lock = threading.Lock()
        self.allowed = 0
        self.denied = 0
        self.m_deny = METRICS.counter(f"ratelimit_{name}_denied_total", "限流拒绝")

    def _refill(self) -> None:
        now = self.clock.monotonic()
        delta = max(0.0, now - self._last)
        self._tokens = min(self.burst, self._tokens + delta * self.rate)
        self._last = now

    def try_acquire(self, n: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                self.allowed += 1
                return True
            self.denied += 1
            self.m_deny.inc()
            return False

    def acquire(self, n: float = 1.0, wait_s: float = 0.0, poll_s: float = 0.01) -> bool:
        """等一小会儿再拿令牌。

        ``try_acquire`` 是"直接拒绝"（服务端对客户端的正确姿势）；``acquire``
        是"在允许的等待窗口内平滑一下"（客户端对自己下游的合理姿势）。

        生产上的取舍：**允许等待 = 削峰但增加延迟；直接拒绝 = 低延迟但增加错误**。
        等待窗口必须很短（一般 < 200ms），否则限流就退化成排队。
        """
        deadline = time.perf_counter() + max(0.0, wait_s)
        while True:
            if self.try_acquire(n):
                return True
            if time.perf_counter() >= deadline:
                return False
            need = min(self.retry_after_s(n), max(0.0, deadline - time.perf_counter()), poll_s)
            time.sleep(max(0.001, need))

    def retry_after_s(self, n: float = 1.0) -> float:
        with self._lock:
            self._refill()
            need = max(0.0, n - self._tokens)
            return need / self.rate if self.rate > 0 else 1.0

    @property
    def tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens

    def stats(self) -> str:
        return f"{self.name}: allowed={self.allowed} denied={self.denied} tokens={self.tokens:.1f}"


class SlidingWindowLimiter:
    """滑动窗口计数限流。比令牌桶更"精确"，适合配额（QPS/分钟配额）。"""

    def __init__(self, limit: int, window_s: float, clock: Clock | None = None):
        self.limit = limit
        self.window_s = window_s
        self.clock = clock or RealClock()
        self._events: list[float] = []
        self._lock = threading.Lock()
        self.denied = 0

    def try_acquire(self) -> bool:
        now = self.clock.monotonic()
        with self._lock:
            cutoff = now - self.window_s
            while self._events and self._events[0] < cutoff:
                self._events.pop(0)
            if len(self._events) >= self.limit:
                self.denied += 1
                return False
            self._events.append(now)
            return True

    @property
    def used(self) -> int:
        now = self.clock.monotonic()
        cutoff = now - self.window_s
        return sum(1 for t in self._events if t >= cutoff)


# --------------------------------------------------------------------------
# 异步辅助
# --------------------------------------------------------------------------


async def await_with_deadline(coro, deadline: Deadline, stage: str):
    """在预算内 await；超预算立即抛 BudgetExceeded。"""
    budget_s = deadline.stage_budget_ms(stage) / 1000.0
    if budget_s <= 0:
        raise BudgetExceeded(stage, deadline.total_ms)
    try:
        return await asyncio.wait_for(coro, timeout=budget_s)
    except (asyncio.TimeoutError, TimeoutError):
        raise LLMError.timeout(budget_s) from None


def hedged_call(
    fn: Callable[[], T],
    hedge_after_ms: float,
    max_hedges: int = 1,
    deadline: Deadline | None = None,
) -> T:
    """对冲请求（tail hedging）：P95 优化手段，代价是额外成本。

    注意：**必须配合成本开关**——对冲会把调用量放大，是用钱买尾部延迟。
    """
    results: list[Any] = []
    errors: list[BaseException] = []
    done = threading.Event()
    lock = threading.Lock()

    def worker() -> None:
        try:
            out = fn()
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        else:
            with lock:
                results.append(out)
            done.set()

    threads = [threading.Thread(target=worker, daemon=True)]
    threads[0].start()
    for _ in range(max_hedges):
        if done.wait(hedge_after_ms / 1000.0):
            break
        if deadline is not None and deadline.expired():
            break
        t = threading.Thread(target=worker, daemon=True)
        threads.append(t)
        t.start()
        hedge_after_ms *= 1.0  # 只对冲一次额外请求（教学简化）
        break
    deadline_s = (deadline.remaining_ms() / 1000.0) if deadline else 10.0
    if not done.wait(max(0.0, deadline_s)):
        raise LLMError.timeout(deadline_s)
    with lock:
        if results:
            return results[0]
        raise errors[0] if errors else LLMError.timeout(deadline_s)
