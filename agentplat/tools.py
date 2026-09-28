"""Capstone 工具层：注册、Schema 校验、超时、幂等、截断、权限、确认。

对应 lab-15 的结论。工具框架必须提供的能力（缺一条就会出事故）：

======================  ==============================================
能力                     缺了会怎样
======================  ==============================================
JSON Schema 参数校验      模型编造参数名/类型 → 脏数据灌进下游
独立超时                  一个慢工具挂死整条链路
返回值截断 + 引用         200KB 结果撑爆上下文，挤掉关键约束
幂等键                    重试导致重复下单/重复发通知
权限声明 + 强制校验        工具越权访问（LLM 不该参与授权判断）
副作用确认声明            human-in-the-loop 缺失
最大迭代 + 重复调用检测    无限工具循环烧钱
======================  ==============================================
"""

from __future__ import annotations

import re
import json
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

from agentlab.metrics import METRICS
from agentlab.tokens import count_tokens

from .context import RequestContext
from .runtime import BoundedCallbacks, invoke_checked


# --------------------------------------------------------------------------
# 精简 JSON Schema 校验器
# --------------------------------------------------------------------------


from .schema import SchemaError, validate_schema

# --------------------------------------------------------------------------
# 工具定义
# --------------------------------------------------------------------------


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ToolResult:
    ok: bool
    value: str
    truncated: bool = False
    ref: str = ""
    tokens: int = 0
    latency_ms: float = 0.0
    attempts: int = 1
    error: str = ""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., str]
    timeout_s: float = 2.0
    max_retries: int = 0
    idempotent: bool = True
    requires_roles: frozenset[str] = field(default_factory=frozenset)
    requires_confirmation: bool = False
    has_side_effects: bool = False
    max_output_tokens: int = 600
    max_calls_per_request: int = 4

    def describe(self) -> str:
        """生成给 LLM 的工具描述。

        lab-15 的结论：**描述质量直接决定工具选择正确率**。所以
        description 必须写清"什么时候用它、什么时候不要用它、参数含义"。
        """
        props = self.parameters.get("properties", {})
        args = ", ".join(
            f"{k}:{v.get('type', 'any')}{'*' if k in self.parameters.get('required', []) else ''}"
            for k, v in props.items()
        )
        flags = []
        if not self.idempotent:
            flags.append("非幂等")
        if self.has_side_effects:
            flags.append("有副作用")
        if self.requires_confirmation:
            flags.append("需人工确认")
        if self.requires_roles:
            flags.append("需角色:" + ",".join(sorted(self.requires_roles)))
        suffix = f" [{'; '.join(flags)}]" if flags else ""
        return f"{self.name}({args}) — {self.description}{suffix}"


class ToolRegistry:
    """工具注册表 + 调用网关。所有安全与可靠性约束都在**这一层强制**。"""

    def __init__(self, cfg=None) -> None:
        self.cfg = cfg
        self._tools: dict[str, Tool] = {}
        self._callbacks = BoundedCallbacks(8)
        self._key_locks = {}
        self._uncertain = set()
        self._refs = {}
        self._lock = threading.RLock()
        self._idem: dict[str, str] = {}
        # 每请求的调用计数与重复检测
        self._calls: dict[str, dict[str, int]] = {}
        self.invalid_args = 0
        self.timeouts = 0
        self.denied = 0
        self.duplicates_blocked = 0
        self.truncated = 0
        self.m_invalid = METRICS.counter("tool_invalid_args_total", "参数校验失败")
        self.m_timeout = METRICS.counter("tool_timeout_total", "工具超时")
        self.m_denied = METRICS.counter("tool_denied_total", "权限拒绝")
        self.m_dup = METRICS.counter("tool_duplicate_blocked_total", "重复调用拦截")
        self.m_latency = METRICS.histogram("tool_latency_ms", "工具耗时")

    # -- 注册 ---------------------------------------------------------------
    def register(self, tool: Tool) -> None:
        with self._lock:
            self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        t = self._tools.get(name)
        if t is None:
            raise ToolError("UNKNOWN_TOOL", f"不存在工具 {name}；可用工具：{sorted(self._tools)}")
        return t

    def describe_all(self) -> str:
        return "\n".join(t.describe() for t in self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools)

    # -- 调用 ---------------------------------------------------------------
    def call(self, name, args, ctx, request_id="", confirmed=False):
        key = json.dumps([ctx.tenant_id, request_id or ctx.trace_id, name, args], sort_keys=True)
        with self._lock:
            lock = self._key_locks.setdefault(key, threading.Lock())
        with lock:
            if key in self._uncertain:
                raise ToolError('OUTCOME_UNKNOWN', '上次执行结果未知，禁止自动重试')
            try:
                return self._call(name, args, ctx, request_id, confirmed)
            except ToolError as exc:
                if exc.code == 'TIMEOUT':
                    self._uncertain.add(key)
                raise

    def _call(
        self,
        name: str,
        args: dict,
        ctx: RequestContext,
        request_id: str = "",
        confirmed: bool = False,
    ) -> ToolResult:
        t0 = time.perf_counter()
        ctx.assert_valid()
        tool = self.get(name)
        rid = request_id or ctx.trace_id

        # 1) 迭代上限：防无限工具循环（工程硬编码，不问模型）
        with self._lock:
            used = self._calls.setdefault(rid, {})
            used[name] = used.get(name, 0) + 1
            total = sum(used.values())
            if used[name] > tool.max_calls_per_request:
                self.duplicates_blocked += 1
                self.m_dup.inc()
                raise ToolError(
                    "LOOP_GUARD",
                    f"工具 {name} 在本次请求内已调用 {used[name] - 1} 次，"
                    f"超过上限 {tool.max_calls_per_request}：疑似循环，已拦截",
                )
            if total > 12:
                self.duplicates_blocked += 1
                self.m_dup.inc()
                raise ToolError("LOOP_GUARD", f"本次请求工具调用总数 {total} 超过上限 12")

        # 2) 权限：角色校验在工程层，不看模型怎么说
        if tool.requires_roles and not (tool.requires_roles & ctx.roles):
            self.denied += 1
            self.m_denied.inc()
            raise ToolError(
                "FORBIDDEN",
                f"用户 {ctx.user_id} 角色 {sorted(ctx.roles)} 无权调用 {name}"
                f"（需要 {sorted(tool.requires_roles)}）",
            )

        # 3) 参数校验：结构化错误信息，便于回灌给模型重试
        try:
            validate_schema(args, tool.parameters)
        except SchemaError as exc:
            self.invalid_args += 1
            self.m_invalid.inc()
            raise ToolError("INVALID_ARGS", str(exc)) from None

        # 4) 副作用确认
        if tool.requires_confirmation and not confirmed:
            raise ToolError(
                "NEEDS_CONFIRMATION", f"{name} 有副作用，需要人工确认后才能执行"
            )

        # 5) 幂等：非幂等工具必须有幂等键，且重复键直接返回上次结果
        idem_key = ""
        if not tool.idempotent or tool.has_side_effects:
            idem_key = hashlib.sha256(json.dumps([ctx.tenant_id, rid, name, args], sort_keys=True).encode()).hexdigest()
            with self._lock:
                prev = self._idem.get(idem_key)
            if prev is not None:
                self.duplicates_blocked += 1
                self.m_dup.inc()
                return ToolResult(
                    ok=True,
                    value=prev,
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                    ref=f"idem:{idem_key[:8]}",
                )

        # 6) 超时：用独立线程池，绝不让慢工具拖住主链路
        attempt = 0
        last: BaseException | None = None
        while attempt <= tool.max_retries:
            attempt += 1
            try:
                value = self._callbacks.call(
                    lambda: invoke_checked(name, args, tool.parameters, tool.fn), tool.timeout_s)
                break
            except FutureTimeout:
                self.timeouts += 1
                self.m_timeout.inc()
                raise ToolError("TIMEOUT", f"{name} 等待超时，结果未知；未声称执行已停止") from None
            except TypeError as exc:
                self.invalid_args += 1
                self.m_invalid.inc()
                raise ToolError("INVALID_ARGS", f"参数不匹配：{exc}") from None
            except BaseException as exc:  # noqa: BLE001
                last = exc
        else:
            raise last if last else ToolError("TOOL_FAILED", f"{name} 失败")

        # 7) 截断 + 引用：大结果不进上下文，只留摘要 + ref
        text = str(value)
        tokens = count_tokens(text)
        truncated = False
        ref = ""
        if tokens > tool.max_output_tokens:
            truncated = True
            self.truncated += 1
            ref = 'toolcache:' + hashlib.sha256(text.encode()).hexdigest()
            self._refs[ref] = text
            keep = max(120, tool.max_output_tokens * 3)
            text = text[:keep] + f"…[截断，完整结果见 {ref}，共 {tokens} tokens]"
            tokens = count_tokens(text)

        latency_ms = (time.perf_counter() - t0) * 1000.0
        self.m_latency.observe(latency_ms)
        if idem_key:
            with self._lock:
                self._idem[idem_key] = text
        return ToolResult(
            ok=True, value=text, truncated=truncated, ref=ref,
            tokens=tokens, latency_ms=latency_ms, attempts=attempt,
        )

    def read_result(self, ref: str, offset=0, max_chars=4000):
        return self._refs[ref][offset:offset + max_chars]

    def new_request(self, request_id: str) -> None:
        with self._lock:
            self._calls.pop(request_id, None)

    def stats(self) -> str:
        return (
            f"tools={len(self._tools)} invalid_args={self.invalid_args} "
            f"timeouts={self.timeouts} denied={self.denied} "
            f"dup_blocked={self.duplicates_blocked} truncated={self.truncated}"
        )


# --------------------------------------------------------------------------
# 标准工具集
# --------------------------------------------------------------------------


def build_default_registry(cfg=None, index=None) -> ToolRegistry:
    reg = ToolRegistry(cfg)

    def search_kb(query: str, top_k: int = 4) -> str:
        if index is None:
            return "（知识库未初始化）"
        from agentlab.store import Query

        res = index.search(Query(query, top_k))
        return " | ".join(f"[ref:{h.doc.doc_id}] {h.text}" for h in res.hits)

    def calculator(expression: str) -> str:
        # 安全考虑：不用 eval；只支持四则运算 + 括号
        if not re.fullmatch(r"[0-9+\-*/(). %]+", expression):
            raise ToolError("UNSAFE_EXPR", "表达式包含不允许的字符")
        allowed = {"__builtins__": {}}
        try:
            val = eval(expression, allowed, {})  # noqa: S307 - 已做字符白名单
        except ZeroDivisionError:
            raise ToolError("DIV_ZERO", "除数为 0") from None
        except Exception as exc:  # noqa: BLE001
            raise ToolError("BAD_EXPR", f"表达式无法计算：{exc}") from None
        return f"{val}"

    def http_fetch(url: str, timeout_s: float = 0.5) -> str:
        # 模拟第三方接口：可能慢、可能失败
        if "slow" in url:
            time.sleep(timeout_s * 3)
        if "fail" in url:
            raise ToolError("UPSTREAM_5XX", f"{url} 返回 503")
        return f"（模拟响应）{url} 的内容 " + "x" * 40

    _db: dict[str, str] = {}

    def write_db(key: str, value: str) -> str:
        _db[key] = value
        return f"written {key}"

    def send_notification(user: str, text: str) -> str:
        send_notification.calls += 1  # type: ignore[attr-defined]
        return f"已通知 {user}（第 {send_notification.calls} 次）"  # type: ignore[attr-defined]

    send_notification.calls = 0  # type: ignore[attr-defined]

    reg.register(
        Tool(
            name="search_kb",
            description=(
                "在内部知识库中检索资料。**当问题涉及公司制度、技术方案、历史工单时使用**；"
                "闲聊或纯计算不要用。query 用关键词而不是完整句子效果更好。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "maxLength": 200},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            fn=search_kb,
            timeout_s=1.0,
            idempotent=True,
        )
    )
    reg.register(
        Tool(
            name="calculator",
            description="做精确的数值计算（四则运算）。**涉及金额、比例、容量计算时必须用它**，不要自己心算。",
            parameters={
                "type": "object",
                "properties": {"expression": {"type": "string", "maxLength": 200}},
                "required": ["expression"],
                "additionalProperties": False,
            },
            fn=calculator,
            timeout_s=0.3,
            idempotent=True,
        )
    )
    reg.register(
        Tool(
            name="http_fetch",
            description="抓取外部网页/接口内容。**只在需要外部实时信息时使用**；内部知识优先用 search_kb。",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "pattern": r"https?://.*", "maxLength": 300},
                    "timeout_s": {"type": "number", "minimum": 0.1, "maximum": 5.0},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            fn=http_fetch,
            timeout_s=0.6,
            max_retries=1,
            idempotent=True,
            max_output_tokens=300,
        )
    )
    reg.register(
        Tool(
            name="write_db",
            description="写入内部数据库。**有副作用，必须提供 idempotency 语义**；不确定时不要调用。",
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "maxLength": 64},
                    "value": {"type": "string", "maxLength": 500},
                },
                "required": ["key", "value"],
                "additionalProperties": False,
            },
            fn=write_db,
            timeout_s=0.5,
            idempotent=False,
            has_side_effects=True,
        )
    )
    reg.register(
        Tool(
            name="send_notification",
            description="给用户发送通知。**有副作用且会打扰用户**，仅在用户明确要求时调用。",
            parameters={
                "type": "object",
                "properties": {
                    "user": {"type": "string", "maxLength": 64},
                    "text": {"type": "string", "maxLength": 300},
                },
                "required": ["user", "text"],
                "additionalProperties": False,
            },
            fn=send_notification,
            timeout_s=0.5,
            idempotent=False,
            has_side_effects=True,
            requires_confirmation=True,
            requires_roles=frozenset({"operator"}),
        )
    )
    return reg
