"""模拟 LLM Provider：把"第三方 LLM 不稳定"这件事变成可控实验。

它刻意复刻了真实线上会遇到的行为：

* **有并发上限**（每个模型一个连接池/配额），超过就排队，排队太深直接 429；
* **排队也在计时**，所以客户端超时会把排队时间算进去 —— 这是"限流+超时"
  互相放大的根因；
* **延迟是对数正态**，尾部很重 —— 平均值好看但 P95 崩掉；
* **随机 503 / 5xx 抖动**；
* **前缀缓存**命中后延迟下降、计费按 10%；
* **token 计量**按输入/输出/命中分别记账；
* **hang 模式**：模拟对端"半死不活"——连上了但永不返回（最危险的一类故障）。

关键教学点：服务端**永远不替客户端做超时判断**。你不设超时，它就会一直挂着。
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Sequence

from .metrics import METRICS
from .tokens import (
    LADDER,
    LARGE,
    MID,
    MODELS,
    SMALL,
    CostLedger,
    ModelSpec,
    count_messages,
    count_tokens,
)
from .util import lognormal_latency

# 重新导出，让 lab 可以只 import providers 就拿到模型阶梯。
# （docs/CONTRACT.md 第 4 节承诺了 `from agentlab.providers import SMALL, MID, LARGE`，
#   文档承诺的可用性必须由代码保证，否则照文档写就会 ImportError。）
__all__ = [
    "SMALL",
    "MID",
    "LARGE",
    "LADDER",
    "MODELS",
    "ChatMessage",
    "LLMReply",
    "LLMServer",
    "LLMError",
    "CircuitOpen",
    "BudgetExceeded",
    "Usage",
    "user",
    "system",
    "assistant",
    "tool_msg",
    "default_server",
]


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str
    name: str = ""
    #: 函数调用协议字段。编码 agent 需要它：assistant 消息里带 tool_calls
    #: （模型说"我要调这些工具"），随后每个工具结果以 role="tool" 回灌。
    #: 用 OpenAI 原生协议而不是"让模型输出 JSON 表示要调什么"，是因为
    #: 协议由服务端约束，比 prompt 约束可靠得多。
    tool_calls: list[dict] = field(default_factory=list)
    tool_call_id: str = ""

    def __str__(self) -> str:  # 便于 lab 打印
        if self.tool_calls:
            names = ",".join(tc.get("function", {}).get("name", "?") for tc in self.tool_calls)
            return f"{self.role}:<tool_calls {names}>"
        return f"{self.role}:{self.content[:40]}"

    def to_api(self) -> dict:
        """转成 OpenAI 兼容的消息体。"""
        if self.role == "tool":
            return {"role": "tool", "content": self.content,
                    "tool_call_id": self.tool_call_id}
        if self.tool_calls:
            return {"role": "assistant", "content": self.content or None,
                    "tool_calls": self.tool_calls}
        return {"role": self.role, "content": self.content}


@dataclass
class Usage:
    in_tokens: int = 0
    out_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total(self) -> int:
        return self.in_tokens + self.out_tokens


@dataclass
class LLMReply:
    text: str
    model: str
    usage: Usage
    latency_ms: float
    cached: bool = False
    queued_ms: float = 0.0
    attempts: int = 1
    raw: dict = field(default_factory=dict)

    @property
    def content(self) -> str:
        return self.text


class LLMError(Exception):
    """Provider 返回的错误。``code`` 用于分类统计与重试判定。"""

    def __init__(
        self,
        code: str,
        message: str = "",
        retry_after: float = 0.0,
        retryable: bool = False,
    ):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retry_after = retry_after
        self.retryable = retryable

    def __str__(self) -> str:
        suffix = f" retry_after={self.retry_after:.2f}s" if self.retry_after else ""
        return f"[{self.code}] {self.message}{suffix}"

    # 常用工厂
    @classmethod
    def rate_limited(cls, retry_after: float = 1.0, detail: str = "") -> "LLMError":
        return cls("429", f"rate limited. {detail}".strip(), retry_after, retryable=True)

    @classmethod
    def unavailable(cls, detail: str = "") -> "LLMError":
        return cls("503", f"upstream unavailable. {detail}".strip(), 0.3, retryable=True)

    @classmethod
    def timeout(cls, budget_s: float) -> "LLMError":
        return cls("TIMEOUT", f"exceeded {budget_s * 1000:.0f}ms budget", 0.0, retryable=True)

    @classmethod
    def bad_request(cls, detail: str = "") -> "LLMError":
        return cls("400", f"bad request. {detail}".strip(), 0.0, retryable=False)


class CircuitOpen(LLMError):
    """本地熔断器打开时抛出，不是 provider 返回的。"""

    def __init__(self, name: str, remaining_s: float):
        super().__init__("CIRCUIT_OPEN", f"{name} 熔断中，剩余 {remaining_s:.1f}s", remaining_s, False)
        self.name = name


class BudgetExceeded(LLMError):
    """整条请求链路的预算耗尽。"""

    def __init__(self, stage: str, budget_ms: float):
        super().__init__("BUDGET", f"stage={stage} 超出预算 {budget_ms:.0f}ms", 0.0, False)
        self.stage = stage


# --------------------------------------------------------------------------
# 服务端
# --------------------------------------------------------------------------


class LLMServer:
    """进程内的 LLM provider 模拟器，线程安全 + 协程安全。"""

    def __init__(
        self,
        models: Sequence[ModelSpec] | None = None,
        max_queue: int = 64,
        max_wait_s: float = 5.0,
        seed: int = 7,
        json_failure_rate: float = 0.18,
    ):
        self.models = {m.name: m for m in (models or LADDER)}
        self.max_queue = max_queue
        # ``max_wait_s`` 保留为"调用方不传 timeout 时的兜底总预算"
        self.default_timeout_s = max_wait_s
        self.max_wait_s = max_wait_s
        # 模型不按结构化输出契约回答的概率（用于演示输出契约的必要性）
        self.json_failure_rate = json_failure_rate
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        # 每模型的并发闸门：Condition + 计数，比 Semaphore 更容易做"超时即拒绝"
        self._inflight: dict[str, int] = {name: 0 for name in self.models}
        self._cond = threading.Condition(self._lock)
        self.ledger = CostLedger()
        self.stats: dict[str, float] = {
            "requests": 0,
            "ok": 0,
            "cache_hits": 0,
            "rejected_429": 0,
            "server_errors": 0,
            "inflight_now": 0,
            "queued_now": 0,
            "max_inflight": 0,
            "max_queued": 0,
            "total_queue_ms": 0.0,
            "client_timeouts": 0,
            "orphaned_workers": 0,
            "hangs_inflight": 0,
            "cache_bytes_hint": 0.0,
        }
        # 前缀缓存：(model, prefix_key) -> 命中的输入 token 数
        self._prefix_cache: dict[tuple[str, str], int] = {}
        self._hangs: set[str] = set()
        self._hang_seconds: dict[str, float] = {}
        self._extra_latency_ms: dict[str, float] = {}
        # 指标（延迟单位统一用 ms）
        self.m_latency = METRICS.histogram("llm_latency_ms", "provider 端到端耗时")
        self.m_queue = METRICS.histogram("llm_queue_ms", "provider 排队耗时")
        self.m_inflight = METRICS.gauge("llm_inflight", "当前在飞请求")
        self.m_queued = METRICS.gauge("llm_queued", "当前排队请求")
        self.m_calls = METRICS.counter("llm_calls_total", "调用次数")
        self.m_by_model = METRICS.counter("llm_calls_by_model_total", "按模型调用次数")
        self.m_tokens_in = METRICS.counter("llm_input_tokens_total", "输入 token")
        self.m_tokens_out = METRICS.counter("llm_output_tokens_total", "输出 token")
        self.m_cache = METRICS.counter("llm_prefix_cache_hits_total", "前缀缓存命中")
        self.m_reject = METRICS.counter("llm_rejected_429_total", "被限流拒绝")
        self.m_5xx = METRICS.counter("llm_5xx_total", "上游 5xx")
        self.m_timeout = METRICS.counter("llm_client_timeout_total", "客户端预算耗尽")
        self.m_usd = METRICS.counter("llm_cost_usd_total", "累计花费（美元）")

    # -- 控制面（lab 用来制造故障） -----------------------------------------
    def set_error_rate(self, model: str, rate: float) -> None:
        """热调某个模型的错误率（模拟上游抖动加剧）。"""
        spec = self.models[model]
        self.models[model] = ModelSpec(
            name=spec.name,
            tier=spec.tier,
            latency_p50_ms=spec.latency_p50_ms,
            latency_sigma=spec.latency_sigma,
            quality=spec.quality,
            in_price=spec.in_price,
            out_price=spec.out_price,
            max_parallel=spec.max_parallel,
            error_rate=rate,
            supports_prefix_cache=spec.supports_prefix_cache,
        )

    def set_latency(self, model: str, p50_ms: float, sigma: float | None = None) -> None:
        spec = self.models[model]
        self.models[model] = ModelSpec(
            name=spec.name,
            tier=spec.tier,
            latency_p50_ms=p50_ms,
            latency_sigma=sigma if sigma is not None else spec.latency_sigma,
            quality=spec.quality,
            in_price=spec.in_price,
            out_price=spec.out_price,
            max_parallel=spec.max_parallel,
            error_rate=spec.error_rate,
            supports_prefix_cache=spec.supports_prefix_cache,
        )

    def hang(self, model: str, on: bool = True, duration_s: float = 3.0) -> None:
        """模拟"连上了但永不返回"的对端。这类故障不设超时就会拖垮线程池。

        ``duration_s`` 默认只挂 3 秒（教学用），设成很大的值就等价于真挂死。
        它**故意不理会调用方的 timeout**：真实世界的挂死对端也不会替你收尾，
        只能靠调用方自己的超时/熔断自救。
        """
        with self._lock:
            if on:
                self._hangs.add(model)
                self._hang_seconds[model] = duration_s
            else:
                self._hangs.discard(model)
                self._hang_seconds.pop(model, None)

    def warm_prefix(self, model: str, prefix_key: str, tokens: int) -> None:
        """预热前缀缓存（模拟 prompt 前缀已落在 provider 侧）。"""
        with self._lock:
            self._prefix_cache[(model, prefix_key)] = tokens

    def clear_prefix_cache(self) -> None:
        with self._lock:
            self._prefix_cache.clear()

    def reset_stats(self, clear_ledger: bool = True) -> None:
        """清零统计。

        ``clear_ledger=True``（默认）会**连成本账本一起清零** —— 这是刻意的：
        如果只清 ``stats`` 而留下 ``ledger``，同一个 server 实例重跑实验时
        "调用次数归零但累计花费还在涨"，算出来的单价会离谱地高，而且看不出错在哪。
        这与 ``METRICS.reset()`` 的语义保持一致：reset 就是把观测状态清干净。

        需要保留累计花费做跨场景对账时，显式传 ``clear_ledger=False``，
        或者自己在重跑前记下 ``srv.ledger.usd`` 做基线差值。
        """
        with self._lock:
            for k in self.stats:
                self.stats[k] = 0.0
            if clear_ledger:
                self.ledger = CostLedger()

    # -- 缓存 key -----------------------------------------------------------
    @staticmethod
    def prefix_key(messages: Sequence[ChatMessage]) -> str:
        """稳定前缀 = **第一条 system 消息**。

        这是个容易踩的坑：如果把"所有连续的 system 消息"都算进前缀，那么
        时间戳、检索结果、会话历史（它们通常是另外的 system 消息）就会污染
        前缀，导致**前缀缓存 100% miss** —— 而且你不会收到任何报错，只是账单
        悄悄变贵。真实 provider 的 prefix cache 也是这样：只有稳定的那一段
        才能复用。所以工程约定是：**稳定内容放在第一条 system 消息里，
        易变内容一律往后放**（见 ``PromptPrefixBuilder``）。
        """
        for m in messages:
            if m.role == "system":
                return f"s:{m.content}"
            break
        return ""

    @staticmethod
    def prefix_tokens(messages: Sequence[ChatMessage]) -> int:
        """稳定前缀占多少 token（= 可以按缓存价计费的部分）。"""
        for m in messages:
            if m.role == "system":
                return count_tokens(m.content) + 4
            break
        return 0

    # -- 内部：延迟采样 -----------------------------------------------------
    def _sample_latency_ms(self, spec: ModelSpec) -> float:
        base = lognormal_latency(self._rng, spec.latency_p50_ms, spec.latency_sigma)
        extra = self._extra_latency_ms.get(spec.name, 0.0)
        return base + extra

    def _classify(self, messages: Sequence[ChatMessage], model: str) -> tuple[int, int, int]:
        """返回 (in_tokens, out_tokens, cached_tokens)。

        ``cached_tokens`` 是**真正命中前缀缓存**的部分：只有当 (model, prefix_key)
        已经预热过时才计入。它决定了计费（缓存部分按 10%）和延迟（TTFT 下降）。
        """
        in_tokens = count_messages(messages)
        # 输出长度按最后一条消息长度粗估
        last = messages[-1].content if messages else ""
        out_tokens = max(16, min(1200, count_tokens(last) // 2 + 48))
        key = self.prefix_key(messages)
        with self._lock:
            cached = self._prefix_cache.get((model, key), 0)
        return in_tokens, out_tokens, min(cached, in_tokens)

    # -- 同步调用 -----------------------------------------------------------
    def call(
        self,
        messages: Sequence[ChatMessage],
        model: str = MID.name,
        timeout: float | None = None,
        tenant: str = "default",
        tag: str = "",
    ) -> LLMReply:
        """同步调用。

        ``timeout`` 是**这一次调用的全部预算**（含排队）。传 ``None`` 时用
        ``default_timeout_s``（默认 5s）—— provider 永远不会无限期挂着调用方，
        但你也绝不能依赖这个默认值：真实 provider 没有这么好心。
        """
        spec = self.models.get(model)
        if spec is None:
            raise LLMError.bad_request(f"unknown model {model}")
        budget = self.default_timeout_s if timeout is None else timeout
        t0 = time.perf_counter()
        wait_deadline = t0 + max(0.0, budget)

        with self._lock:
            if self._hangs and spec.name in self._hangs:
                # 挂死：记录"对端还有活没干完"。**注意这里绝不能动 ``_inflight``**：
                # 并发槽由下面那段统一 +1、由 finally 统一 -1，在这里再 +1 会导致
                # 挂死调用每次净 +1，模型会变成"永久假满载"，把后续所有正常调用
                # 都挤成 429。这个 bug 真出现过，而且症状完全不像挂死功能的问题。
                self.stats["hangs_inflight"] += 1
            while self._inflight[spec.name] >= spec.max_parallel:
                if self.stats["queued_now"] >= self.max_queue:
                    self.stats["rejected_429"] += 1
                    self.m_reject.inc()
                    raise LLMError.rate_limited(
                        retry_after=0.5,
                        detail=f"{spec.name} inflight={self._inflight[spec.name]}/{spec.max_parallel} queue={int(self.stats['queued_now'])}/{self.max_queue}",
                    )
                remaining = wait_deadline - time.perf_counter()
                if remaining <= 0:
                    self.stats["rejected_429"] += 1
                    self.m_reject.inc()
                    raise LLMError.rate_limited(
                        retry_after=0.5,
                        detail=f"{spec.name} 排队超时（queue wait 超过客户端预算）",
                    )
                self.stats["queued_now"] += 1
                self.stats["max_queued"] = max(self.stats["max_queued"], self.stats["queued_now"])
                self.m_queued.set(self.stats["queued_now"])
                try:
                    self._cond.wait(timeout=min(remaining, 0.25))
                finally:
                    self.stats["queued_now"] = max(0.0, self.stats["queued_now"] - 1)
                    self.m_queued.set(self.stats["queued_now"])
            self._inflight[spec.name] += 1
            self.stats["inflight_now"] = sum(self._inflight.values())
            self.stats["max_inflight"] = max(
                self.stats["max_inflight"], self.stats["inflight_now"]
            )
            self.m_inflight.set(self.stats["inflight_now"])

        queued_ms = (time.perf_counter() - t0) * 1000.0
        # 真正把 timeout 当预算用：超时就返回 TIMEOUT。
        # 注意实现方式 —— 服务端的"活"不会因为你超时就停下，它会在后台继续跑完，
        # 并且**继续占着并发槽**。这就是真实世界里"客户端早就走了，服务端还被
        # 慢调用占满"的机制，也是必须配熔断的原因。
        result: list[LLMReply] = []
        failure: list[BaseException] = []

        def runner() -> None:
            try:
                result.append(self._serve(spec, messages, queued_ms, tenant, tag))
            except BaseException as exc:  # noqa: BLE001
                failure.append(exc)

        worker = threading.Thread(target=runner, daemon=True)
        worker.start()
        remaining_s = wait_deadline - time.perf_counter()
        worker.join(timeout=max(0.0, remaining_s))

        try:
            if worker.is_alive():
                with self._lock:
                    self.stats["client_timeouts"] += 1
                    self.stats["orphaned_workers"] += 1
                self.m_timeout.inc()
                raise LLMError.timeout(budget)
            if failure:
                raise failure[0]
            if not result:
                raise LLMError.timeout(budget)
            return result[0]
        finally:
            with self._lock:
                self._inflight[spec.name] = max(0, self._inflight[spec.name] - 1)
                self.stats["inflight_now"] = sum(self._inflight.values())
                self.m_inflight.set(self.stats["inflight_now"])
                self._cond.notify_all()

    def _serve(
        self,
        spec: ModelSpec,
        messages: Sequence[ChatMessage],
        queued_ms: float,
        tenant: str,
        tag: str,
    ) -> LLMReply:
        in_tokens, out_tokens, cached = self._classify(messages, spec.name)
        hit = cached > 0
        # 先计量再"计算"：超时/失败也会产生 token 成本 —— 这是重试放大成本的原因
        with self._lock:
            self.stats["requests"] += 1
            self.m_calls.inc()
            self.m_by_model.inc()
            self.m_tokens_in.inc(in_tokens)
            self.m_tokens_out.inc(out_tokens)
            if hit:
                self.stats["cache_hits"] += 1
                self.m_cache.inc()
            self.ledger.add(
                spec,
                in_tokens,
                out_tokens,
                cached,
                tenant=tenant,
                tag=tag,
            )
            self.m_usd.inc(
                (max(0, in_tokens - cached) + cached * 0.1) * spec.in_price
                / 1_000_000
                + out_tokens * spec.out_price / 1_000_000
            )
            hanging = spec.name in self._hangs
            hang_s = self._hang_seconds.get(spec.name, 3.0)

        latency_s = self._sample_latency_ms(spec) / 1000.0
        if hit:  # 前缀命中：TTFT 明显下降
            latency_s *= 0.55
        if hanging:
            # 挂死：睡一个远超任何合理预算的时长，客户端必须靠超时自救
            time.sleep(hang_s)

        time.sleep(latency_s)

        roll = self._rng.random()
        if roll < spec.error_rate:
            with self._lock:
                self.stats["server_errors"] += 1
                self.m_5xx.inc()
            raise LLMError.unavailable(f"{spec.name} 抖动（roll={roll:.3f}）")

        with self._lock:
            self.stats["ok"] += 1
            self.stats["total_queue_ms"] += queued_ms
        total_ms = queued_ms + latency_s * 1000.0
        self.m_latency.observe(total_ms)
        self.m_queue.observe(queued_ms)
        text = self._fake_answer(spec, messages, out_tokens)
        return LLMReply(
            text=text,
            model=spec.name,
            usage=Usage(in_tokens, out_tokens, cached),
            latency_ms=total_ms,
            cached=hit,
            queued_ms=queued_ms,
        )

    def _fake_answer(self, spec: ModelSpec, messages: Sequence[ChatMessage], out_tokens: int) -> str:
        """模拟模型输出。

        默认返回符合"结构化输出契约"的 JSON，但有 ``json_failure_rate`` 的概率
        返回自由文本 —— 这正是真实模型的行为：**你不能假设它一定给你合法 JSON**。
        Agent 侧必须有 Schema 校验 + 修复 + 兜底，否则解析失败就是线上 500。
        """
        q = messages[-1].content if messages else ""
        head = q[:48].replace("\n", " ").replace('"', "'")
        if self._rng.random() < self.json_failure_rate:
            return (
                f"关于「{head}」的回答：这是一段自由文本，不是 JSON。"
                f"（{spec.name}, 约 {out_tokens} tokens）"
            )
        payload = {
            "answer": f"关于「{head}」的回答：模拟输出，质量档位={spec.tier}。",
            "confidence": round(min(0.99, spec.quality + 0.02), 2),
            "citations": [],
            "refused": False,
        }
        return json.dumps(payload, ensure_ascii=False)

    # -- 异步调用 -----------------------------------------------------------
    async def acall(
        self,
        messages: Sequence[ChatMessage],
        model: str = MID.name,
        timeout: float | None = None,
        tenant: str = "default",
        tag: str = "",
    ) -> LLMReply:
        """协程版。concurrency 由 asyncio.Semaphore 保证，配合 ``wait_for`` 做超时。"""
        spec = self.models.get(model)
        if spec is None:
            raise LLMError.bad_request(f"unknown model {model}")
        sem = _async_sem(spec.name, spec.max_parallel)
        t0 = time.perf_counter()
        budget = self.default_timeout_s if timeout is None else timeout
        try:
            await asyncio.wait_for(sem.acquire(), timeout=budget)
        except (asyncio.TimeoutError, TimeoutError):
            self.m_reject.inc()
            raise LLMError.rate_limited(0.5, f"{spec.name} 异步排队超时") from None
        queued_ms = (time.perf_counter() - t0) * 1000.0
        self.m_queue.observe(queued_ms)
        try:
            in_tokens, out_tokens, cached = self._classify(messages, spec.name)
            hit = cached > 0
            with self._lock:
                self.stats["requests"] += 1
                self.m_calls.inc()
                self.m_by_model.inc()
                self.m_tokens_in.inc(in_tokens)
                self.m_tokens_out.inc(out_tokens)
                if hit:
                    self.stats["cache_hits"] += 1
                    self.m_cache.inc()
                self.ledger.add(spec, in_tokens, out_tokens, cached, tenant=tenant, tag=tag)
                hanging = spec.name in self._hangs
                hang_s = self._hang_seconds.get(spec.name, 3.0)
                self.stats["inflight_now"] = sum(self._inflight.values()) + 1
                self.m_inflight.set(self.stats["inflight_now"])
            latency_s = self._sample_latency_ms(spec) / 1000.0 * (0.55 if hit else 1.0)
            sleep_for = hang_s if hanging else latency_s
            try:
                await asyncio.wait_for(asyncio.sleep(sleep_for), timeout=budget)
            except (asyncio.TimeoutError, TimeoutError):
                raise LLMError.timeout(budget) from None
            if self._rng.random() < spec.error_rate:
                with self._lock:
                    self.stats["server_errors"] += 1
                    self.m_5xx.inc()
                raise LLMError.unavailable(f"{spec.name} 抖动")
            with self._lock:
                self.stats["ok"] += 1
            total_ms = queued_ms + latency_s * 1000.0
            self.m_latency.observe(total_ms)
            return LLMReply(
                text=self._fake_answer(spec, messages, out_tokens),
                model=spec.name,
                usage=Usage(in_tokens, out_tokens, cached),
                latency_ms=total_ms,
                cached=hit,
                queued_ms=queued_ms,
            )
        finally:
            sem.release()
            with self._lock:
                self.stats["inflight_now"] = max(0, sum(self._inflight.values()))
                self.m_inflight.set(self.stats["inflight_now"])

    async def acall_with_timeout(self, *args, **kwargs) -> LLMReply:
        timeout = kwargs.pop("timeout", None)
        if timeout is None:
            timeout = 5.0
        return await self.acall(*args, timeout=timeout, **kwargs)

    # -- 可读摘要 -----------------------------------------------------------
    def summary_lines(self) -> list[str]:
        with self._lock:
            s = dict(self.stats)
        return [
            f"provider 请求={int(s['requests'])} 成功={int(s['ok'])} "
            f"缓存命中={int(s['cache_hits'])} 429={int(s['rejected_429'])} 5xx={int(s['server_errors'])}",
            f"并发峰值={int(s['max_inflight'])} 排队峰值={int(s['max_queued'])} "
            f"累计排队={s['total_queue_ms'] / 1000:.2f}s",
            f"客户端超时={int(s['client_timeouts'])} "
            f"其中服务端仍在跑={int(s['orphaned_workers'])}（这些调用照样产生 token 成本）",
            f"token: in={self.ledger.in_tokens} out={self.ledger.out_tokens} "
            f"cached={self.ledger.cached_tokens} usd=${self.ledger.usd:.4f}",
        ]


# asyncio 信号量必须绑定事件循环，按 (loop, model) 缓存
_async_sems: dict[tuple[int, str], asyncio.Semaphore] = {}


def _async_sem(model: str, limit: int) -> asyncio.Semaphore:
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = 0
    key = (loop_id, model)
    sem = _async_sems.get(key)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        _async_sems[key] = sem
    return sem


# --------------------------------------------------------------------------
# 便捷构造
# --------------------------------------------------------------------------


def default_server(**kwargs) -> LLMServer:
    return LLMServer(**kwargs)


def user(text: str) -> ChatMessage:
    return ChatMessage("user", text)


def system(text: str) -> ChatMessage:
    return ChatMessage("system", text)


def assistant(text: str) -> ChatMessage:
    return ChatMessage("assistant", text)


def tool_msg(text: str, name: str = "tool") -> ChatMessage:
    return ChatMessage("tool", text, name)
