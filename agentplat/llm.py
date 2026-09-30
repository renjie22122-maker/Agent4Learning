"""真实 LLM 后端：走任意 **OpenAI 兼容** 端点，零第三方依赖。

设计要点
--------
**它是 `LLMServer` 的子类，只覆写 `_serve()`。** 这一个决定带来三件好处：

1. 并发闸门、排队与 429、客户端超时语义、前缀缓存、成本账本、指标打点
   —— 全部复用父类已经写好且被 17 个 lab 验证过的实现；换成真实后端后，
   我们观察到的仍然是**同一套可靠性行为**，而不是"真实模式下另一套逻辑"。
2. 17 个 lab 用的是内置模拟器（`LLMServer`），**完全不受影响** ——
   真实后端是扩展开关，不是替换。
3. 想接新厂商只需要改 base_url / 模型名，不用碰引擎和 UI。

只覆盖 `_serve()` 而不是重写 `call()`，是因为父类的 `call()` 里有真正重要的
逻辑：并发槽的获取与释放、排队深度与 429、客户端超时后"服务端仍在跑"的
隔离语义。那些才是生产行为，必须原样保留。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Sequence

from agentlab.metrics import METRICS
from agentlab.providers import ChatMessage, LLMError, LLMReply, LLMServer, Usage
from agentlab.tokens import MID, ModelSpec, count_messages, count_tokens

from .llmconfig import LLMConfig


from .llm_errors import LLMCallError, _http_status_to_llm_error
from .llm_protocol import PROTOCOL_REPAIRS, sanitize_messages, sanitize_in_place


from .openai_chat import OpenAIChatClient, _hint_for


class RealLLMServer(LLMServer):
    """把真实 OpenAI 兼容端点接进平台，但**保留全部可靠性机制**。"""

    def __init__(self, cfg: LLMConfig, guard=None):
        self.llm_cfg = cfg
        self.guard = guard
        self.dry_run = bool(getattr(guard, "dry_run", False))
        self.coalesced = 0  # 被合并掉的重复调用数（省下的真实请求）
        # ⚠ 命名警告：**不要叫 `_inflight`**。
        # 父类 `LLMServer` 已经用 `self._inflight` 存"每个模型的并发计数"
        # （`dict[str, int]`）。子类若用同名属性去存合并表（`dict[key, _PendingCall]`），
        # 就会把父类那个字典整个覆盖掉，于是父类里的
        #     sum(self._inflight.values())
        # 变成 int + _PendingCall，直接抛
        #     TypeError: unsupported operand type(s) for +: 'int' and '_PendingCall'
        #
        # 这个 bug 真发生过。教训：**继承时新增状态前，先确认父类没有同名属性**；
        # 子类私有状态统一加业务前缀（这里是 _coalesce_*），不要图省事沿用通用名。
        self._coalesce: dict[tuple, "_PendingCall"] = {}
        self._coalesce_lock = threading.Lock()
        tier = cfg.tier_map()
        specs = [
            ModelSpec(
                name="small-8b", tier="small",
                latency_p50_ms=800, quality=0.7,
                in_price=cfg.price_in_per_m, out_price=cfg.price_out_per_m,
                max_parallel=cfg.max_parallel,
            ),
            ModelSpec(
                name="mid-32b", tier="mid",
                latency_p50_ms=1200, quality=0.85,
                in_price=cfg.price_in_per_m, out_price=cfg.price_out_per_m,
                max_parallel=cfg.max_parallel,
            ),
            ModelSpec(
                name="large-400b", tier="large",
                latency_p50_ms=2000, quality=0.95,
                in_price=cfg.price_in_per_m, out_price=cfg.price_out_per_m,
                max_parallel=max(1, cfg.max_parallel // 2),
            ),
        ]
        super().__init__(specs, max_queue=64, max_wait_s=cfg.timeout_s, seed=7)
        from .model_client import create_client
        self.client = create_client(cfg)
        self.real_calls = 0
        self.real_errors = 0
        self.fallbacks = 0
        self._tier_models = tier
        self.m_real = METRICS.counter("real_llm_served_total", "真实 LLM 成功应答")
        self.m_fallback = METRICS.counter("real_llm_fallback_total", "退回模拟器次数")
        self.m_coalesced = METRICS.counter(
            "real_llm_coalesced_total", "并发重复请求被合并（省下的真实调用）"
        )

    # -- 覆写这一处即可 ------------------------------------------------
    #: 只有**暂时性**故障才退回模拟器。401/403/404/400 是配置错误，
    #: 退回模拟器会把"key 错了"伪装成"模型答得怪"，用户永远查不出问题。
    #: 宁可让请求失败并显示真实原因，也不要给一个看起来正常的假答案。
    TRANSIENT_CODES = ("429", "503", "TIMEOUT", "502")

    def _serve(
        self,
        spec: ModelSpec,
        messages: Sequence[ChatMessage],
        queued_ms: float,
        tenant: str,
        tag: str,
    ) -> LLMReply:
        model_name = self._tier_models.get(spec.name, self.llm_cfg.model)
        # 变量名必须区分开：下面退回分支会把"加了说明的副本"传给父类，
        # 若直接覆盖 messages，父类再读 messages[-1].content 就会炸
        # （实测踩过：AttributeError: 'list' object has no attribute 'content'）。
        req_messages = list(messages)
        in_tokens = count_messages(req_messages)
        cached = self._classify(req_messages, spec.name)[2]

        with self._lock:
            self.stats["requests"] += 1
            self.m_calls.inc()
            self.m_by_model.inc()
            self.m_tokens_in.inc(in_tokens)

        # ---- 成本护栏：必须在**出网之前**检查，事后统计没有意义 ----
        if self.guard is not None:
            self.guard.preflight(in_tokens, tag=tag or spec.name)
            if self.dry_run:
                # 干跑模式：不发出真实请求，返回一个明确标注的占位结果。
                # 这样批量实验可以先跑一遍看调用量与估算花费，再决定是否真跑。
                return LLMReply(
                    text='{"answer": "[dry-run] 未发起真实请求", "confidence": 0.0}',
                    model=f"{model_name}(dry-run)",
                    usage=Usage(in_tokens, 8, cached),
                    latency_ms=queued_ms,
                    cached=False,
                    queued_ms=queued_ms,
                    raw={"dry_run": True, "tier": spec.name},
                )

        # ---- 并发请求合并（singleflight）----
        # 10 个用户同时问同一句话时，只发 1 次真实请求，其余等结果。
        # 这是真实后端下**最直接的省钱手段**（缓存只能挡住"先后到达"的重复，
        # 挡不住"同时到达"的重复）。
        key = (model_name, self.llm_cfg.temperature, hash(tuple(
            (m.role, m.content) for m in req_messages
        )))
        with self._coalesce_lock:
            pending = self._coalesce.get(key)
            if pending is None:
                pending = _PendingCall()
                self._coalesce[key] = pending
                leader = True
            else:
                leader = False

        if not leader:
            # wait() 在领头失败时会原样抛出同一个异常 —— 等待者不会各自重发，
            # 所以上游出问题时并发请求数不会从 1 被放大成 N。
            text, usage = pending.wait(self.llm_cfg.timeout_s + 5)
            with self._lock:
                self.coalesced += 1
                self.stats["ok"] += 1
            self.m_coalesced.inc()
            self.ledger.add(spec, usage.in_tokens, usage.out_tokens, cached,
                            tenant=tenant, tag=f"{tag}:coalesced")
            self.m_usd.inc(
                (max(0, usage.in_tokens - cached) + cached * 0.1) * spec.in_price / 1e6
                + usage.out_tokens * spec.out_price / 1e6
            )
            return LLMReply(
                text=text, model=model_name, usage=usage,
                latency_ms=queued_ms + pending.elapsed_ms, cached=False,
                queued_ms=queued_ms,
                raw={"coalesced": True, "tier": spec.name},
            )

        t0 = time.perf_counter()
        try:
            text, usage = self.client.complete(
                model_name, req_messages, self.llm_cfg.timeout_s
            )
        except LLMError as exc:
            with self._coalesce_lock:
                self._coalesce.pop(key, None)
            pending.fail(exc)
            code = str(getattr(exc, "code", "ERR"))
            transient = code in self.TRANSIENT_CODES
            with self._lock:
                self.real_errors += 1
                self.stats["server_errors"] += 1
                self.m_5xx.inc()
                if transient and self.llm_cfg.offline_mock_fallback:
                    self.fallbacks += 1
            # 失败也记账：这些调用同样消耗了并发窗口与上行流量
            self.ledger.add(spec, in_tokens, 0, cached, tenant=tenant,
                            tag=f"{tag}:failed")
            if not (transient and self.llm_cfg.offline_mock_fallback):
                # 配置类错误，或用户关掉了兜底 → 如实抛出，让上层熔断/降级处理
                raise
            self.m_fallback.inc()
            note = (
                f"[真实 LLM 暂时不可用，本条由内置模拟器代答] {code}: {str(exc)[:160]}"
            )
            fallback = super()._serve(
                spec, _prepend_note(req_messages, note), queued_ms, tenant, tag
            )
            fallback.raw["fallback_reason"] = str(exc)[:300]
            return fallback

        total_ms = queued_ms + (time.perf_counter() - t0) * 1000.0
        # 让等待中的并发请求拿到同一份结果（singleflight 收尾）
        with self._coalesce_lock:
            self._coalesce.pop(key, None)
        pending.resolve(text, usage)
        if self.guard is not None:
            self.guard.record(
                usage.in_tokens, usage.out_tokens,
                self.llm_cfg.price_in_per_m, self.llm_cfg.price_out_per_m,
                tag=tag or spec.name,
            )
        with self._lock:
            self.real_calls += 1
            self.stats["ok"] += 1
            self.stats["total_queue_ms"] += queued_ms
            self.m_tokens_out.inc(usage.out_tokens)
            if cached:
                self.stats["cache_hits"] += 1
                self.m_cache.inc()
        self.m_real.inc()
        self.m_latency.observe(total_ms)
        self.m_queue.observe(queued_ms)
        self.ledger.add(
            spec, usage.in_tokens, usage.out_tokens, cached, tenant=tenant, tag=tag
        )
        self.m_usd.inc(
            (max(0, usage.in_tokens - cached) + cached * 0.1) * spec.in_price / 1_000_000
            + usage.out_tokens * spec.out_price / 1_000_000
        )
        return LLMReply(
            text=text,
            model=f"{model_name}",  # 显示真实模型名，而不是内部档位名
            usage=usage,
            latency_ms=total_ms,
            cached=cached > 0,
            queued_ms=queued_ms,
            raw={"real": True, "tier": spec.name},
        )

    # -- 界面需要的诊断 ------------------------------------------------
    def summary_lines(self) -> list[str]:
        base = super().summary_lines()
        return [
            f"后端=真实 LLM  成功={self.real_calls}  失败={self.real_errors}  "
            f"退回模拟器={self.fallbacks}",
            f"端点={self.llm_cfg.chat_url()}",
            *base,
        ]

    def probe(self, tier: str = "mid") -> dict:
        model = self._tier_models.get(f"{tier}", self.llm_cfg.model)
        return self.client.probe(model)


def _prepend_note(messages: Sequence[ChatMessage], note: str) -> list[ChatMessage]:
    """给退回模拟器的调用加一条说明，让用户明确知道"这条不是真实模型答的"。"""
    if not messages:
        return [ChatMessage("system", note)]
    out = list(messages)
    first = out[0]
    if first.role == "system":
        out[0] = ChatMessage("system", f"{note}\n{first.content}")
    else:
        out.insert(0, ChatMessage("system", note))
    return out


class _PendingCall:
    """一次在飞的真实调用，供并发请求共享结果（singleflight）。

    为什么要它：缓存只挡得住"**先后**到达"的重复请求，挡不住"**同时**到达"的。
    10 个用户同时问同一句话时，没有合并就会打出 10 次真实 API 调用 ——
    这是真实 key 下最直接、最容易忽略的浪费。
    """

    __slots__ = ("_ev", "_result", "_error", "started_at")

    def __init__(self) -> None:
        self._ev = threading.Event()
        self._result: tuple[str, Usage] | None = None
        self._error: BaseException | None = None
        self.started_at = time.perf_counter()

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000.0

    def resolve(self, text: str, usage: Usage) -> None:
        self._result = (text, usage)
        self._ev.set()

    def fail(self, exc: BaseException) -> None:
        self._error = exc
        self._ev.set()

    def wait(self, timeout_s: float) -> tuple[str, Usage]:
        """等领头请求完成。

        * 成功 → 返回 ``(text, usage)``，**每个等待者拿到同一份结果**。
        * 领头失败 → **原样抛出同一个异常**，而不是返回 None 让等待者各自重发。
          为什么这点重要：等待者失败后若各自重发，N 个并发请求就变成
          1 + N 次上游调用 —— 合并白做了；而且上游正出问题时被我们 N 倍放大，
          恰好是最该收敛的时刻。
        * 超时 → 抛 ``LLMError.timeout``，交给上层正常超时路径处理。
        """
        if not self._ev.wait(max(1.0, timeout_s)):
            raise LLMError.timeout(timeout_s)
        if self._error is not None:
            raise self._error
        if self._result is None:
            raise LLMError("EMPTY", "合并调用没有返回结果", 0.0, retryable=True)
        return self._result


def build_server(cfg: LLMConfig, guard=None) -> LLMServer:
    """按配置返回后端：真实 LLM 或内置模拟器。

    ``guard`` 只对真实后端生效 —— 模拟器不花钱，不需要护栏。
    """
    if cfg.is_real:
        return RealLLMServer(cfg, guard=guard)
    return LLMServer(max_queue=64, max_wait_s=30.0, seed=7)


__all__ = [
    "LLMConfig",
    "OpenAIChatClient",
    "RealLLMServer",
    "build_server",
    "MID",
    "LLMCallError",
]
