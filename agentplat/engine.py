"""Capstone 主链路：一次请求的完整生命周期。

这是把前面所有 lab 的结论串起来的地方。执行顺序本身就是一个结论：

    鉴权/上下文 → 限流 → 舱壁 → 缓存 → 检索 → 上下文组装
      → 模型路由 → LLM（超时+重试+熔断）→ 工具（有界循环）→ 校验 → 回写缓存

为什么是这个顺序：
* 限流和舱壁放最前 —— 最便宜的拒绝方式就是"根本不开始干活"；
* 缓存放检索之前 —— 命中了就不该花钱检索；
* 路由放上下文组装之后 —— 因为复杂度依赖 prompt 的真实 token 数；
* 校验放最后但**不可省略** —— 模型输出必须过 Schema 才能交给下游。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from agentlab.metrics import METRICS
from agentlab.orchestration import (
    Bulkhead,
    Deadline,
    RetryBudget,
    call_with_retry,
)
from agentlab.providers import BudgetExceeded, CircuitOpen, LLMError, LLMServer, system, user
from agentlab.store import Query
from agentlab.tokens import MODELS, count_tokens
from agentlab.tracing import Tracer

from .cache import CacheSystem, jaccard
from .context import (
    BuiltContext,
    ContextBuilder,
    IsolationError,
    RequestContext,
    SessionStore,
)
from .resilience import (
    BreakerRegistry,
    BulkheadRegistry,
    ModelRouter,
    RateLimiterStack,
    build_retry_budget,
    build_retry_policy,
)
from .tools import ToolError, ToolRegistry


@dataclass
class AgentRequest:
    query: str
    ctx: RequestContext
    #: 是否把内部知识库检索结果作为参考资料注入。
    #:
    #: **默认 False**：agent 首先是通用的，"有资料就参考"是加分项而不是前提。
    #: 早期版本默认 True，导致每个请求都把检索片段硬塞进 prompt，
    #: 叠加当时那条"无依据即拒答"的 system prompt，agent 就退化成只会念文档的
    #: 检索器 —— 连"写个俄罗斯方块"都拒答。需要 RAG 的场景（内部制度问答）
    #: 显式打开即可。
    use_retrieval: bool = False
    #: 是否允许调用工具（计算器/检索等）。注意它与 use_retrieval 是**两件事**：
    #: 工具是 agent 主动去取信息，检索注入是我们在组装 prompt 时替他取好。
    needs_tools: bool = False
    #: 人格：general（通用助手）/ strict_rag（严格检索，无依据即拒答）
    persona: str = "general"
    confirmed_tools: frozenset[str] = field(default_factory=frozenset)
    truth: str | None = None  # 仅压测用：语义缓存"错误命中"的判据


@dataclass
class AgentResponse:
    ok: bool
    answer: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cached: bool = False
    cache_layer: str = ""
    latency_ms: float = 0.0
    usd: float = 0.0
    error: str = ""
    http_status: int = 200
    degraded: bool = False
    tool_calls: int = 0
    retries: int = 0
    stage_ms: dict[str, float] = field(default_factory=dict)
    context_tokens: int = 0
    #: 本请求的 span 树快照：(名称, 耗时ms, 状态, 属性)。服务化之后必须能
    #: "事后回看单次请求干了什么" —— 否则线上排查只能靠猜（lab-18 的教训）。
    spans: list[tuple[str, float, str, dict]] = field(default_factory=list)
    trace_id: str = ""

    def render_trace(self) -> None:
        print(f"\n  ┌─ trace {self.trace_id}  （总耗时 {self.latency_ms:.0f}ms）")
        for name, ms, status, attrs in self.spans:
            flag = "" if status == "OK" else f"  !{status}"
            extra = ""
            if attrs:
                extra = "  " + " ".join(f"{k}={v}" for k, v in list(attrs.items())[:4])
            print(f"  │ {name:<14} {ms:8.1f}ms{flag}{extra}")
        print("  └" + "─" * 62)

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "model": self.model,
                "tokens": {"in": self.tokens_in, "out": self.tokens_out},
                "cached": self.cached,
                "latency_ms": round(self.latency_ms, 1),
                "usd": round(self.usd, 6),
                "error": self.error,
                "degraded": self.degraded,
            },
            ensure_ascii=False,
        )


# --------------------------------------------------------------------------
# 输出契约：结构化输出 + 校验 + 修复重试
# --------------------------------------------------------------------------


ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "maxLength": 4000},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "citations": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "refused": {"type": "boolean"},
    },
    "required": ["answer", "confidence"],
    "additionalProperties": False,
}


class OutputContract:
    """把模型自由文本变成受 Schema 约束的结构化结果。

    工程铁律：**永远不要相信模型会按要求返回 JSON**。必须校验、必须修复、
    必须有兜底。解析失败率是要打点观测的核心指标。
    """

    def __init__(self) -> None:
        self.parse_failures = 0
        self.repairs = 0
        self.fallbacks = 0
        self.m_parse_fail = METRICS.counter("output_parse_failure_total", "结构化输出解析失败")
        self.m_repair = METRICS.counter("output_repair_total", "结构化输出修复尝试")

    @staticmethod
    def extract_json(text: str) -> dict | None:
        # 1) 直接解析
        with __import__("contextlib").suppress(Exception):
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        # 2) 从 markdown 代码块里抠
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        if m:
            with __import__("contextlib").suppress(Exception):
                return json.loads(m.group(1))
        # 3) 抠第一个平衡的大括号
        start = text.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        with __import__("contextlib").suppress(Exception):
                            return json.loads(text[start : i + 1])
                        break
        return None

    def coerce(self, text: str, allow_repair: bool = True) -> dict:
        """返回符合 schema 的 dict；失败则走兜底（**绝不抛给用户 500**）。

        关于"思考过程泄漏"：实测某些模型会把 reasoning 和正文一起吐出来
        （"用户要求...我应该...让我写代码：```python ...```"），此时若把整段
        文本当答案，用户看到的就是模型的内心独白而不是答案本身。

        所以修复策略是有序的：
        1. 能解析出 JSON → 用其中的 answer 字段（最可靠）；
        2. 解析不出来 → 从自由文本里**剥掉明显的思考段落**再当答案，
           而不是原样透传。宁可答案短一点，也不要把独白当答案给用户。
        """
        obj = self.extract_json(text)
        if obj is None:
            self.parse_failures += 1
            self.m_parse_fail.inc()
            if allow_repair:
                self.repairs += 1
                self.m_repair.inc()
                # 处理顺序很重要，实测踩过：
                #   模型常见输出 = 一段内心独白 + 后接 JSON，而 JSON 又可能被
                #   max_tokens 截断。所以要**先剥独白、再抢救 JSON 正文**。
                # 反过来做的话，独白开头的文本里没有 `"answer"` 字段，
                # 抢救直接放弃，最后把"用户要求…我应该…"整段当答案给用户。
                cleaned = _strip_reasoning(text)
                salvaged = _salvage_answer(cleaned)
                answer = salvaged if salvaged else cleaned
                return {"answer": answer[:4000], "confidence": 0.3, "refused": False}
            self.fallbacks += 1
            return {"answer": "", "confidence": 0.0, "refused": False}
        from .tools import SchemaError, validate_schema

        try:
            validate_schema(obj, ANSWER_SCHEMA)
            return obj
        except SchemaError:
            self.parse_failures += 1
            self.m_parse_fail.inc()
            if allow_repair:
                self.repairs += 1
                self.m_repair.inc()
                inner = str(obj.get("answer", "")) or text
                return {
                    "answer": _strip_reasoning(inner)[:4000],
                    "confidence": 0.25,
                    "refused": bool(obj.get("refused", False)),
                }
            self.fallbacks += 1
            return {"answer": "", "confidence": 0.0, "refused": False}


def _salvage_answer(text: str) -> str | None:
    """从**被截断的 JSON** 里抢救出 answer 字段。

    实测场景：写代码类问题输出很长，撞上 max_tokens 上限，JSON 在字符串中间
    断掉（`{"answer": "def clear_lines(...`）。此时 `json.loads` 必然失败，
    若直接把整段文本当答案，用户看到的是 `{"answer": "……` 这样一串带转义的碎片。

    这里用正则把 answer 的开头部分捞出来，至少给出**可读的正文**。
    这是典型的"降级要好于原样透传"：截断的答案仍然有用，原始 JSON 碎片没用。
    """
    if not text:
        return None
    m = re.search(r'"answer"\s*:\s*"', text)
    if not m:
        return None
    body = text[m.end():]
    # 逐步尝试：先按正常 JSON 字符串结束符切，失败就去掉尾部残留再切
    out_chars: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            # 只还原因转义产生的换行/引号，其余保持原样
            out_chars.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            break  # 字符串正常结束
        out_chars.append(ch)
        i += 1
    salvaged = "".join(out_chars).strip()
    return salvaged if len(salvaged) >= 20 else None


#: 思考过程的典型开场白。模型（尤其是被要求 JSON 输出时）经常先自言自语
#: 一段"我应该/让我/用户要求"，再接真正的内容。这些段落要剥掉。
_REASONING_MARKERS = (
    "用户要求", "用户问", "我应该", "让我", "我需要", "我需要先", "首先我",
    "The user wants", "We need", "I should", "Let me", "I need to", "The user asks",
    "Known facts", "Previous context",
)


def _strip_reasoning(text: str) -> str:
    """从自由文本里剥掉明显的"内心独白"段落。

    做法：按行处理，丢掉那些以思考标记开头、且**不含代码块/列表结构**的行。
    保守策略 —— 只删明显是独白的行，避免误删真正的答案内容。
    """
    if not text:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    in_code = False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            kept.append(ln)
            continue
        if in_code:
            kept.append(ln)
            continue
        if any(stripped.startswith(m) for m in _REASONING_MARKERS):
            continue
        kept.append(ln)
    out = "\n".join(kept).strip()
    # 如果剥完几乎什么都不剩，说明整段都是独白 —— 那就保留原文，
    # 至少让用户看到内容，而不是收到一个空回答。
    return out if len(out) >= 20 else text.strip()


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------


class AgentEngine:
    def __init__(
        self,
        cfg,
        server: LLMServer,
        index,
        platform=None,
    ) -> None:
        self.cfg = cfg
        self.server = server
        self.index = index
        self.platform = platform  # Service：用于 readiness / 停机检查

        self.sessions = SessionStore()
        self.context = ContextBuilder(cfg)
        self.cache = CacheSystem(cfg)
        self.breakers = BreakerRegistry(cfg)
        self.bulkheads = BulkheadRegistry(cfg)
        self.ratelimit = RateLimiterStack(cfg)
        self.router = ModelRouter(cfg, self.breakers, server.ledger)
        self.tools = ToolRegistry(cfg)
        self.output = OutputContract()
        self.retry_policy = build_retry_policy(cfg)
        self.retry_budget = build_retry_budget(cfg)

        # 指标
        self.m_requests = METRICS.counter("agent_requests_total", "请求总数")
        self.m_ok = METRICS.counter("agent_ok_total", "成功请求")
        self.m_err = METRICS.counter("agent_error_total", "失败请求")
        self.m_rt = METRICS.histogram("agent_latency_ms", "端到端延迟")
        self.m_over_budget = METRICS.counter("agent_over_budget_total", "超出 SLO 的请求")
        self.m_tool_calls = METRICS.counter("agent_tool_calls_total", "工具调用次数")
        self.m_llm_calls = METRICS.counter("agent_llm_calls_total", "LLM 调用次数")
        self.m_cache_hit = METRICS.counter("agent_cache_hit_total", "缓存命中")

    # -- 检索 ---------------------------------------------------------------
    def _retrieve(self, ctx: RequestContext, query: str, top_k: int = 5):
        """权限过滤下推到召回阶段（filter at source）——不是召回后再过滤。"""
        q = Query(
            text=query,
            top_k=top_k,
            tenant=ctx.tenant_id,
            groups=ctx.groups | frozenset({"public"}),
        )
        return self.index.search(q)

    # -- 主流程 -------------------------------------------------------------
    def handle(self, req: AgentRequest) -> AgentResponse:
        t0 = time.perf_counter()
        ctx = req.ctx
        self.m_requests.inc()
        tracer = Tracer(ctx.trace_id)
        resp = AgentResponse(ok=False)
        # 成本归因：用 **租户级** 账本的前后差值，而不是"这条请求调了几次模型"
        # 的估算。差值天然包含了重试、二次生成、失败调用产生的花费 ——
        # 而这些恰恰是账单里最容易被漏掉的部分。
        usd_before = self.server.ledger.by_tenant.get(ctx.tenant_id, 0.0)
        # 租户上下文显式校验：缺 tenant/user/session 直接拒绝，不猜默认值
        try:
            ctx.assert_valid()
        except IsolationError as exc:
            resp.error = f"ISOLATION: {exc}"
            resp.http_status = 400
            resp.latency_ms = (time.perf_counter() - t0) * 1000.0
            self.m_err.inc()
            return resp

        deadline = Deadline.root(self.cfg.request_budget_ms, self.cfg.stage_budget_ms, name=ctx.trace_id)
        bulkheads_held: list[Bulkhead] = []

        try:
            with tracer.span("rate_limit") as sp:
                self.ratelimit.check(ctx.tenant_id, self._initial_model_hint(req))
                sp.set(tenant=ctx.tenant_id)

            with tracer.span("bulkhead") as sp:
                held = self._acquire_bulkheads(ctx)
                bulkheads_held = held
                sp.set(pools=len(held))

            # 1) 缓存（最便宜的成功路径）
            with tracer.span("cache") as sp:
                answer, layer = self._try_cache(req)
                if answer is not None:
                    resp.ok = True
                    resp.answer = answer
                    resp.cached = True
                    resp.cache_layer = layer
                    resp.model = "cache"
                    self.m_cache_hit.inc()
                    sp.set(layer=layer)
                    resp.latency_ms = (time.perf_counter() - t0) * 1000.0
                    self._finish_metrics(resp)
                    return resp

            # 2) 检索（仅在显式开启 RAG 时做）
            #
            # 通用 agent 不该无条件检索：绝大多数问题（写代码、解释概念、做分析）
            # 根本不需要知识库，白检索既费时间又把无关片段塞进 prompt 干扰模型。
            # 需要 RAG 的场景由调用方通过 use_retrieval=True 显式声明。
            res = None
            if req.use_retrieval:
                with tracer.span("retrieve") as sp:
                    t_r = time.perf_counter()
                    rkey = self.cache.exact_key(ctx.tenant_id, f"retrieve::{req.query}", "index")
                    cached_hits = self.cache.tools.get(rkey)
                    if cached_hits is not None and self.index is not None:
                        from agentlab.store import Hit, RetrievalResult

                        hits = [
                            Hit(self.index.docs[int(i)], 1.0, "cache")
                            for i in cached_hits.split(",")[:5]
                            if i.isdigit()
                        ]
                        res = RetrievalResult(hits, len(hits), 0, 0.0)
                    else:
                        res = self._retrieve(ctx, req.query)
                        if self.index is not None:
                            self.cache.tools.put(
                                rkey,
                                ",".join(h.doc.doc_id.lstrip("d") for h in res.hits),
                                tenant=ctx.tenant_id,
                            )
                    sp.set(candidates=res.candidates, hits=len(res.hits))
                    resp.stage_ms["retrieve"] = (time.perf_counter() - t_r) * 1000.0
            else:
                with tracer.span("retrieve") as sp:
                    sp.set(skipped="通用模式，未检索")

            # 3) 会话 + 上下文组装
            with tracer.span("context") as sp:
                session = self.sessions.load(ctx)
                # 人格按请求切换：通用助手 vs 严格检索。
                # 它是 prompt 的一部分（属于稳定前缀），所以换人格会让前缀缓存失效 ——
                # 这是正确的代价，不能为了缓存命中而把两种人格混在一个前缀里。
                if req.persona != self.context.persona:
                    self.context.persona = req.persona
                    self.context.system_prompt = self.context.prompt_for(req.persona)
                built = self.context.build(ctx, session, res.hits if res else [], req.query)
                resp.context_tokens = built.tokens
                sp.set(tokens=built.tokens, dropped=built.dropped_turns,
                       persona=req.persona)

            # 4) 路由
            with tracer.span("route") as sp:
                decision = self.router.initial_route(
                    ctx.tenant_id,
                    built.tokens,
                    req.needs_tools,
                    # 多跳的判据要保守：检索命中多不代表难，把普通检索问句判成
                    # "难"会把大部分流量推给最贵的档位，白白烧钱（lab-13 的教训）
                    multi_hop=bool(res and res.candidates > 20 and len(res.hits) >= 5),
                )
                resp.model = decision.model
                resp.degraded = decision.downgraded or decision.fallback_level > 0
                sp.set(model=decision.model, reason=decision.reason[:40])

            # 5) LLM：超时 + 重试 + 熔断
            with tracer.span("llm") as sp:
                text, model_used, usage, retries = self._call_llm(req, built, decision, deadline, ctx)
                resp.model = model_used
                resp.tokens_in = usage[0]
                resp.tokens_out = usage[1]
                resp.retries = retries
                sp.set(model=model_used, retries=retries)

            # 6) 工具循环（有界）
            tool_calls = 0
            if req.needs_tools:
                with tracer.span("tools") as sp:
                    text, tool_calls = self._maybe_use_tools(req, built, text, deadline, ctx)
                    sp.set(calls=tool_calls)
            resp.tool_calls = tool_calls

            # 7) 输出契约
            with tracer.span("finalize") as sp:
                parsed = self.output.coerce(text)
                answer = str(parsed.get("answer", "")).strip()
                if not answer:
                    raise LLMError("EMPTY_OUTPUT", "模型输出为空", 0.0, retryable=True)
                resp.answer = answer
                resp.ok = True
                sp.set(confidence=parsed.get("confidence", 0.0))

            # 8) 回写缓存 + 会话
            self._store_results(req, ctx, answer, session)
            resp.latency_ms = (time.perf_counter() - t0) * 1000.0
            self._finish_metrics(resp)
            return resp

        except (CircuitOpen, BudgetExceeded, LLMError) as exc:
            resp.ok = False
            resp.error = f"{getattr(exc, 'code', 'ERR')}: {exc}"
            resp.http_status = self._status_of(exc)
            resp.degraded = True
        except IsolationError as exc:
            resp.ok = False
            resp.error = f"ISOLATION: {exc}"
            resp.http_status = 403
        except ToolError as exc:
            resp.ok = False
            resp.error = f"TOOL_{exc.code}: {exc.message}"
            resp.http_status = 400 if exc.code in ("INVALID_ARGS", "FORBIDDEN") else 503
        except Exception as exc:  # noqa: BLE001 - 兜底：绝不把异常泄漏成 500 堆栈
            resp.ok = False
            resp.error = f"UNEXPECTED: {type(exc).__name__}: {exc}"
            resp.http_status = 500
        finally:
            for bh in bulkheads_held:
                bh.release()
            # **失败请求也必须记录真实耗时与真实成本**。只统计成功请求的延迟是
            # 自欺欺人：超时/被拒的请求同样占用了用户等待时间，还照样产生 token
            # 账单。把它们算成 0 会让 P95 和成本看起来很美，而体验和账单都很差。
            resp.latency_ms = (time.perf_counter() - t0) * 1000.0
            resp.usd = self._cost_delta(ctx, usd_before)
            resp.trace_id = ctx.trace_id
            resp.spans = [
                (s.name, round(s.duration_ms, 1), s.status, dict(s.attrs))
                for s in tracer.all_spans
            ]
            self._finish_metrics(resp)
            if not resp.ok:
                self.m_err.inc()
        return resp

    def _cost_delta(self, ctx: RequestContext, before: float) -> float:
        """租户账本差值（并发下同一租户的请求会互相包含，故取不小于 0 的估计）。"""
        after = self.server.ledger.by_tenant.get(ctx.tenant_id, 0.0)
        return max(0.0, after - before)

    # -- 内部步骤 -----------------------------------------------------------
    @staticmethod
    def _initial_model_hint(req: AgentRequest) -> str:
        """限流阶段还不知道最终模型，用 mid 作为占位（真实实现会用更粗的桶）。"""
        return "mid-32b"

    def _acquire_bulkheads(self, ctx: RequestContext) -> list[Bulkhead]:
        held: list[Bulkhead] = []
        if not self.bulkheads.global_pool.acquire(wait_s=0.05):
            raise LLMError.rate_limited(0.05, "全局并发已满（快速失败，不排队）")
        held.append(self.bulkheads.global_pool)
        if ctx.is_batch and not self.bulkheads.batch_pool.acquire(wait_s=0.02):
            raise LLMError.rate_limited(0.05, "批处理并发配额已满")
        if ctx.is_batch:
            held.append(self.bulkheads.batch_pool)
        tp = self.bulkheads.tenant_pool(ctx.tenant_id)
        if not tp.acquire(wait_s=0.05):
            raise LLMError.rate_limited(0.05, f"租户 {ctx.tenant_id} 并发已满")
        held.append(tp)
        return held

    def _try_cache(self, req: AgentRequest) -> tuple[str | None, str]:
        """返回 (答案, 命中的层)。层名如实标注，否则没法判断是哪一层在起作用。

        persona 与 use_retrieval 必须传进去 —— 它们都会改变答案，
        漏掉任何一个都会让开关"看起来没生效"（实测踩过）。
        """
        return self.cache.lookup(
            req.ctx.tenant_id, req.query, self.cfg.routing_mode, req.truth,
            persona=req.persona, use_retrieval=req.use_retrieval,
        )

    def _call_llm(
        self,
        req: AgentRequest,
        built: BuiltContext,
        decision,
        deadline: Deadline,
        ctx: RequestContext,
    ) -> tuple[str, str, tuple[int, int], int]:
        """带分层超时、重试、熔断的 LLM 调用。返回 (文本, 实际模型, (in,out), 重试次数)。"""
        state = {"retries": 0, "model": decision.model, "usage": (0, 0)}

        def attempt() -> str:
            model = state["model"]
            breaker = self.breakers.get(f"llm:{model}")
            with deadline.stage("llm") as st:

                def once() -> str:
                    st.check()
                    # 限流走"小额平滑等待"：把限流从制造错误变成削峰。
                    # 窗口 150ms，远小于 llm 阶段预算，所以不会吃掉超时预算。
                    try:
                        self.ratelimit.check(ctx.tenant_id, model, wait_s=0.15)
                    except LLMError as exc:
                        if exc.code == "429":
                            time.sleep(min(exc.retry_after, 0.1))  # 尊重 Retry-After
                        raise
                    reply = self.server.call(
                        built.messages,
                        model=model,
                        timeout=st.timeout_s(floor_s=0.12),
                        tenant=ctx.tenant_id,
                        tag="agent",
                    )
                    if reply.cached:
                        self.cache.account_saving(reply.latency_ms * 0.45, 0.0)
                    state["usage"] = (reply.usage.in_tokens, reply.usage.out_tokens)
                    return reply.text

                try:
                    text = breaker.call(once)
                except LLMError as exc:
                    # 503/429/超时 → 先试升级模型（如果还没到顶），再交给重试策略
                    nxt = self.router.should_escalate(
                        0.2 if exc.retryable else 0.9, state["model"]
                    )
                    if nxt and exc.retryable:
                        state["model"] = nxt
                    raise
                return text

        def on_retry(attempt_no: int, exc: BaseException, delay: float) -> None:
            state["retries"] += 1

        text = call_with_retry(
            attempt,
            self.retry_policy,
            deadline=deadline,
            budget=self.retry_budget,
            on_retry=on_retry,
        )
        self.m_llm_calls.inc()
        # 注意：**不在生成阶段记账**。成本统一由 provider 的 CostLedger 记录，
        # 这里只把租户维度的花费同步给路由器的预算闸门（两边记两次就会翻倍）。
        spec = MODELS.get(state["model"])
        if spec:
            est = (
                state["usage"][0] * spec.in_price + state["usage"][1] * spec.out_price
            ) / 1_000_000
            self.router.record_cost(ctx.tenant_id, est)
        return text, state["model"], state["usage"], state["retries"]

    def _maybe_use_tools(
        self,
        req: AgentRequest,
        built: BuiltContext,
        text: str,
        deadline: Deadline,
        ctx: RequestContext,
    ) -> tuple[str, int]:
        """有界的工具使用：**触发条件、次数上限、循环检测全部硬编码**。

        生产上这一步通常是 LLM 决定要不要调工具；这里用确定性规则替代，
        因为 capstone 要的是"可压测、可复现"，而不是"聪明"。
        """
        calls = 0
        wanted: list[tuple[str, dict]] = []
        if "计算" in req.query or any(ch.isdigit() for ch in req.query) and "?" in req.query:
            m = re.search(r"([0-9][0-9+\-*/(). ]{2,})", req.query)
            if m:
                wanted.append(("calculator", {"expression": m.group(1).strip()}))
        if len(req.query) > 8 and req.needs_tools:
            wanted.append(("search_kb", {"query": req.query[:60], "top_k": 3}))

        wanted = wanted[:2]
        if not wanted:
            return text, 0
        try:
            with deadline.stage("tools"):
                results: list[str] = []
                for name, args in wanted:
                    st_budget = deadline.stage_budget_ms("tools") / 1000.0
                    if st_budget <= 0.05:
                        break
                    confirmed = name in req.confirmed_tools
                    try:
                        r = self.tools.call(
                            name,
                            args,
                            ctx,
                            request_id=ctx.trace_id,
                            confirmed=confirmed,
                        )
                        results.append(f"{name} → {r.value}")
                        calls += 1
                        self.m_tool_calls.inc()
                    except ToolError as exc:
                        # 工具失败是**可观测事件**，不是崩溃：把结构化错误回灌给模型
                        results.append(f"{name} 调用失败[{exc.code}]: {exc.message}")
        except BudgetExceeded:
            return text, calls

        if not results:
            return text, calls
        # 二次生成：把工具结果并进上下文（仍然走同一套预算与熔断）
        follow = BuiltContext(
            messages=built.messages + [system("[工具结果]\n" + "\n".join(results))],
            prefix_text=built.prefix_text,
            volatile_text=built.volatile_text,
            tokens=built.tokens,
            kept_turns=built.kept_turns,
            dropped_turns=built.dropped_turns,
            summarized=built.summarized,
            facts_kept=built.facts_kept,
        )
        model = self.router.initial_route(ctx.tenant_id, follow.tokens, False, False).model
        try:
            with deadline.stage("llm") as st:
                reply = self.server.call(
                    follow.messages,
                    model=model,
                    timeout=st.timeout_s(floor_s=0.12),
                    tenant=ctx.tenant_id,
                    tag="agent:followup",
                )
            self.m_llm_calls.inc()
            return reply.text, calls
        except (LLMError, BudgetExceeded):
            return text, calls  # 二次生成失败就退回第一次的答案，不让整请求失败

    def _store_results(self, req: AgentRequest, ctx: RequestContext, answer: str, session) -> None:
        # 回写缓存时必须带上与 lookup 相同的维度，否则读写的 key 不一致：
        # 写进去的永远命不中，缓存会表现成"完全不生效"。
        self.cache.store(
            ctx.tenant_id, req.query, self.cfg.routing_mode, answer,
            persona=req.persona, use_retrieval=req.use_retrieval,
        )
        # 会话历史只写**摘要**，避免历史无限膨胀；facts 也要有界
        self.sessions.append_turn(ctx, "user", req.query[:200])
        self.sessions.append_turn(ctx, "assistant", answer[:300])
        with self.sessions._lock:  # 会话对象由 SessionStore 统一保护
            if len(session.facts) < 8:
                session.facts[f"q{len(session.turns)}"] = req.query[:24]

    def _status_of(self, exc: BaseException) -> int:
        code = str(getattr(exc, "code", ""))
        return {
            "429": 429,
            "503": 503,
            "TIMEOUT": 504,
            "BUDGET": 504,
            "CIRCUIT_OPEN": 503,
            "EMPTY_OUTPUT": 502,
        }.get(code, 500)

    def _finish_metrics(self, resp: AgentResponse) -> None:
        self.m_rt.observe(resp.latency_ms)
        if resp.ok:
            self.m_ok.inc()
        if resp.latency_ms > self.cfg.slo_p95_ms:
            self.m_over_budget.inc()

    # -- 诊断 ---------------------------------------------------------------
    def render_state(self) -> None:
        self.cache.render()
        self.breakers.render()
        self.bulkheads.render()
        self.ratelimit.render()
        self.router.render()
        print("\n  ┌─ 会话 / 工具 / 输出契约")
        print(f"  │ {self.sessions.stats()}")
        print(f"  │ {self.tools.stats()}")
        print(
            f"  │ 输出契约: 解析失败={self.output.parse_failures} "
            f"修复={self.output.repairs} 兜底={self.output.fallbacks}"
        )
        print("  └" + "─" * 62)
