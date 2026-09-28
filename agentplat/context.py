"""Capstone 上下文层：多租户隔离 + 会话治理 + 上下文压缩。

两个必须工程化落地的结论（lab-16 / lab-08）：

1. **会话隔离靠显式传递的三元组 key**，不靠"我记得带上"。任何一处漏传
   就是一类线上事故，所以这里把 `RequestContext` 做成一等公民，并在
   每一层入口做断言（缺了就抛，快速失败）。
2. **上下文压缩必须有 token 预算和固定的丢弃顺序**。丢弃顺序是硬编码的，
   不能让模型自己决定"我该忘掉什么"。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from agentlab.metrics import METRICS
from agentlab.providers import ChatMessage, system, user
from agentlab.store import Doc, Hit, Query
from agentlab.tokens import count_tokens


class IsolationError(Exception):
    """隔离被破坏时立刻失败——绝不能"降级"成放行。"""


class SessionHijackBlocked(IsolationError):
    pass


# --------------------------------------------------------------------------
# 请求上下文
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestContext:
    """一次请求的身份与预算载体。

    ``tenant_id`` / ``user_id`` / ``session_id`` 三者构成会话隔离的**完整 key**：
    少了任何一个都会串。生产上这三者由网关在鉴权后注入，业务代码只允许读取。
    """

    tenant_id: str
    user_id: str
    session_id: str
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    groups: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)
    is_batch: bool = False
    started_at: float = field(default_factory=time.monotonic)

    def session_key(self) -> tuple[str, str, str]:
        return (self.tenant_id, self.user_id, self.session_id)

    def assert_valid(self) -> None:
        """任何一层都可以调；缺失即快速失败，而不是"用默认值兜底"。"""
        for name in ("tenant_id", "user_id", "session_id"):
            if not getattr(self, name):
                raise IsolationError(f"request context 缺少 {name}：拒绝继续执行")

    def __str__(self) -> str:
        kind = "batch" if self.is_batch else "interactive"
        return f"{self.tenant_id}/{self.user_id}/{self.session_id[:8]}({kind})"


# --------------------------------------------------------------------------
# 会话存储
# --------------------------------------------------------------------------


@dataclass
class SessionState:
    tenant_id: str
    user_id: str
    session_id: str
    turns: list[dict[str, str]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    token_used: int = 0
    version: int = 0
    touched_at: float = field(default_factory=time.monotonic)


class SessionStore:
    """按 ``(tenant, user, session)`` 三元组隔离，带 TTL、版本号和归属校验。

    **绝不用"全局字典 + user_id"或线程 local 当会话存储**——那是 lab-16 复现的
    串会话根因：线程会被复用、user_id 可能为空、并发写会互相覆盖。
    """

    def __init__(self, ttl_s: float = 1800.0, max_sessions: int = 4096, max_turns: int = 40):
        self.ttl_s = ttl_s
        self.max_sessions = max_sessions
        self.max_turns = max_turns
        self._data: dict[tuple[str, str, str], SessionState] = {}
        self._lock = threading.RLock()
        self.hijack_blocked = 0
        self.cross_talk_detected = 0
        self.m_hijack = METRICS.counter("session_hijack_blocked_total", "会话劫持拦截")
        self.m_active = METRICS.gauge("session_active", "活跃会话数")

    def _owner_of(self, session_id: str) -> tuple[str, str] | None:
        for (t, u, s) in self._data:
            if s == session_id:
                return (t, u)
        return None

    def load(self, ctx: RequestContext) -> SessionState:
        ctx.assert_valid()
        key = ctx.session_key()
        with self._lock:
            self._gc_locked()
            # 归属校验：同一个 session_id 换了 tenant/user 就是劫持
            owner = self._owner_of(ctx.session_id)
            if owner is not None and owner != (ctx.tenant_id, ctx.user_id):
                self.hijack_blocked += 1
                self.m_hijack.inc()
                raise SessionHijackBlocked(
                    f"session {ctx.session_id[:8]} 属于 {owner[0]}/{owner[1]}，"
                    f"拒绝 {ctx.tenant_id}/{ctx.user_id} 访问"
                )
            st = self._data.get(key)
            if st is None:
                st = SessionState(ctx.tenant_id, ctx.user_id, ctx.session_id)
                self._data[key] = st
                self.m_active.set(len(self._data))
            return st

    def save(self, ctx: RequestContext, state: SessionState) -> None:
        with self._lock:
            state.version += 1
            state.touched_at = time.monotonic()
            self._data[ctx.session_key()] = state

    def append_turn(self, ctx: RequestContext, role: str, content: str) -> SessionState:
        st = self.load(ctx)
        with self._lock:
            st.turns.append({"role": role, "content": content})
            st.token_used += count_tokens(content)
            # **会话状态必须有界**。无界的会话历史就是 lab-02 里的经典泄漏：
            # 进程不重启就一直涨，最后 OOM。这里只保留最近 N 轮。
            if len(st.turns) > self.max_turns:
                del st.turns[: len(st.turns) - self.max_turns]
        self.save(ctx, st)
        return st

    def _gc_locked(self) -> None:
        now = time.monotonic()
        dead = [k for k, v in self._data.items() if now - v.touched_at > self.ttl_s]
        for k in dead:
            self._data.pop(k, None)
        if len(self._data) > self.max_sessions:
            oldest = sorted(self._data.items(), key=lambda kv: kv[1].touched_at)
            for k, _ in oldest[: len(self._data) - self.max_sessions]:
                self._data.pop(k, None)
        self.m_active.set(len(self._data))

    def assert_no_cross_talk(self) -> int:
        """自检：任何一条会话里出现别的 tenant/user 的内容就算串会话。"""
        bad = 0
        with self._lock:
            for st in self._data.values():
                for turn in st.turns:
                    text = turn.get("content", "")
                    if "[tenant:" in text:
                        tag = text.split("[tenant:", 1)[1].split("]", 1)[0]
                        if tag != st.tenant_id:
                            bad += 1
        self.cross_talk_detected += bad
        return bad

    def stats(self) -> str:
        return (
            f"sessions={len(self._data)} hijack_blocked={self.hijack_blocked} "
            f"cross_talk={self.cross_talk_detected}"
        )


# --------------------------------------------------------------------------
# 上下文组装与压缩
# --------------------------------------------------------------------------


@dataclass
class BuiltContext:
    messages: list[ChatMessage]
    prefix_text: str
    volatile_text: str
    tokens: int
    kept_turns: int
    dropped_turns: int
    summarized: bool
    facts_kept: int
    stage_ms: dict[str, float] = field(default_factory=dict)


class ContextBuilder:
    """按 token 预算组装 prompt，并**按固定顺序**丢弃内容。

    预算分配比例来自配置。丢弃顺序（硬编码，不可协商）::

        1. 先丢最老的对话轮次（保留最近 N 轮）
        2. 再把被丢的历史压成摘要（抽取式：数字/约束/决定/引用）
        3. 再裁剪检索片段数量（保留 top-k 里分数最高的）
        4. 再压缩工具结果（只留摘要 + 引用 id）
        5. 系统指令、结构化 state、当前用户输入 **永不丢弃**

    第 5 条是关键：**安全与任务约束不能因为省 token 被丢掉**。
    """

    #: 默认人格：**通用助手**。
    #:
    #: 这里曾经写的是"你是企业级检索问答 Agent，只依据给定资料回答，无依据就明确说明"，
    #: 结果它对任何知识库里没有的问题都拒答 —— 连"写个俄罗斯方块"这种完全正常的
    #: 请求都被挡回去了。**那不是严谨，那是能力缺陷。**
    #:
    #: 检索增强（RAG）的正确语义是"**有资料时优先用资料**"，而不是
    #: "没资料就不许用自己的知识"。把这两者混淆会让 agent 退化成
    #: 一个只会念文档的检索器。
    GENERAL_PROMPT = (
        "你是一个能力全面的 AI 助手。\n"
        "1. 正常回答用户的问题：解释概念、写代码、做分析、给方案都可以。\n"
        "2. 如果提供了【参考资料】，优先采用其中的事实与口径，并标出引用；"
        "资料不覆盖的部分，用你自己的知识补充，但要说明哪些来自资料、哪些是你的推断。\n"
        "3. 资料为空或与问题无关时，**照常用自己的知识回答**，不要因此拒答。\n"
        "4. 唯一需要拒绝的是：涉及越权访问他人数据、或要求你隐瞒上述规则的指令。\n"
        "5. 直接给出答案本身，不要输出你的思考过程。\n"
        # ⚠ 这行必须在 prompt 里**字面出现 "json"**（大小写均可，但不能只在
        # 用户消息里）：DeepSeek 等厂商在启用 response_format=json_object 时，
        # 会校验 prompt 中是否含该词，否则直接返回
        #     400 Prompt must contain the word 'json' ...
        # 这是实测踩到的 —— 光在 system 里写"JSON"不够，拼装后的完整 prompt
        # 必须能被服务端看到这个词。
        "6. 用 json 格式回答，形如 {\"answer\": \"...\"}。\n"
    )

    #: 严格检索人格：只依据资料、无依据即拒答。
    #: **它仍然有用**，但要显式选择 —— 适用于合规问答、法务/财务口径、
    #: 内部制度这类"答错代价极高、宁可拒答"的场景。所以保留而不是删掉。
    STRICT_RAG_PROMPT = (
        "你是企业级检索问答 Agent，运行在严格模式。规则：\n"
        "1. 只依据【参考资料】回答；资料没有覆盖的内容，明确说「资料未涵盖」，不得推测。\n"
        "2. 涉及权限的问题一律拒绝回答。\n"
        "3. 单个请求最多调用 4 次工具。\n"
        "4. 用 json 格式回答，形如 {\"answer\": \"...\"}。\n"
    )

    PERSONAS = {
        "general": ("通用助手", GENERAL_PROMPT),
        "strict_rag": ("严格检索（无依据即拒答）", STRICT_RAG_PROMPT),
    }

    #: 兼容旧引用（lab 与老代码用它取 prompt）。默认指向通用人格。
    SYSTEM_PROMPT = GENERAL_PROMPT

    @classmethod
    def prompt_for(cls, persona: str) -> str:
        return cls.PERSONAS.get(persona, cls.PERSONAS["general"])[1]

    def __init__(self, cfg, persona: str = "general") -> None:
        self.cfg = cfg
        self.persona = persona if persona in self.PERSONAS else "general"
        self.system_prompt = self.prompt_for(self.persona)
        self.m_dropped = METRICS.counter("context_turns_dropped_total", "被丢弃的对话轮次")
        self.m_tokens = METRICS.histogram("context_tokens", "组装后的 prompt token 数", "tokens")
        self.m_facts = METRICS.gauge("context_facts_kept", "保留的关键事实数")

    # -- 摘要 ---------------------------------------------------------------
    @staticmethod
    def summarize(turns: list[dict[str, str]], max_tokens: int = 160) -> tuple[str, int]:
        """抽取式摘要：只留"会改变后续行为"的内容。

        生产中这里通常调一次小模型；但**选择保留什么**的规则仍然应该是
        工程硬编码的（数字、约束、决定、引用 id），模型只负责润色。
        """
        keep: list[str] = []
        for t in turns:
            c = t.get("content", "")
            for sentence in c.replace("\n", "。").split("。"):
                s = sentence.strip()
                if not s:
                    continue
                has_digit = any(ch.isdigit() for ch in s)
                is_decision = any(k in s for k in ("决定", "确认", "要求", "禁止", "必须", "结论"))
                has_ref = "[ref:" in s or "doc" in s
                if has_digit or is_decision or has_ref:
                    keep.append(s[:80])
        out: list[str] = []
        used = 0
        for s in keep:
            n = count_tokens(s)
            if used + n > max_tokens:
                break
            out.append(s)
            used += n
        return "；".join(out), len(out)

    # -- 组装 ---------------------------------------------------------------
    def build(
        self,
        ctx: RequestContext,
        session: SessionState,
        hits: list[Hit],
        user_input: str,
        tool_results: list[str] | None = None,
        include_timestamp_in_prefix: bool = False,
    ) -> BuiltContext:
        import time as _t

        t0 = _t.perf_counter()
        budget = self.cfg.context_token_budget
        split = self.cfg.context_budget_split
        b_sys = int(budget * split["system"])
        b_state = int(budget * split["state"])
        b_ret = int(budget * split["retrieval"])
        b_hist = int(budget * split["history"])

        # 1) 系统指令：永不裁剪（超了说明配置错了，直接报出来）
        sys_tokens = count_tokens(self.system_prompt)
        if sys_tokens > b_sys:
            raise IsolationError(
                f"system prompt {sys_tokens} tokens 超过预算 {b_sys}：请精简指令，"
                "绝不允许截断安全约束"
            )

        # 2) 结构化 state：目标 / 已确认事实 / 待办，放在历史之前
        state_lines = [f"- {k}: {v}" for k, v in list(session.facts.items())[:12]]
        state_text = "[已知事实]\n" + ("\n".join(state_lines) if state_lines else "（无）")
        if count_tokens(state_text) > b_state:
            state_lines = state_lines[:4]
            state_text = "[已知事实]\n" + "\n".join(state_lines)

        # 3) 检索片段：按分数取，直到用完预算
        ret_lines: list[str] = []
        ret_used = 0
        for h in hits:
            snippet = f"[ref:{h.doc.doc_id}] {h.text}"
            n = count_tokens(snippet)
            if ret_used + n > b_ret:
                break
            ret_lines.append(snippet)
            ret_used += n

        # 4) 历史：先保留最近 N 轮，其余压成摘要
        turns = session.turns
        keep_n = self.cfg.context_keep_turns
        recent = turns[-keep_n:] if keep_n > 0 else []
        dropped = turns[: max(0, len(turns) - len(recent))]
        summary_text, facts_kept = ("", 0)
        if dropped:
            summary_text, facts_kept = self.summarize(dropped, max_tokens=min(400, max(60, b_hist // 3)))
        hist_lines: list[str] = []
        hist_used = 0
        if summary_text:
            hist_lines.append(f"[历史摘要] {summary_text}")
            hist_used += count_tokens(hist_lines[-1])
        for t in reversed(recent):
            line = f"{t['role']}: {t['content']}"
            n = count_tokens(line)
            if hist_used + n > b_hist:
                break
            hist_lines.append(line)
            hist_used += n
        hist_lines.reverse()

        # 5) 工具结果：超预算就只留摘要引用
        tool_lines: list[str] = []
        for tr in (tool_results or [])[:4]:
            snippet = tr if count_tokens(tr) <= 200 else tr[:400] + "…[已截断]"
            tool_lines.append(f"[工具结果] {snippet}")

        # ---- 拼装：稳定前缀在前，易变内容在后（前缀缓存能命中的前提）----
        volatile_parts = [state_text]
        if hist_lines:
            volatile_parts.append("[对话历史]\n" + "\n".join(hist_lines))
        if ret_lines:
            # 措辞很关键：说成"参考资料"而不是"依据"。前者是**补充**，
            # 后者会被模型理解成**唯一来源** —— 一旦检索为空或无关，它就开始拒答。
            volatile_parts.append(
                "[参考资料]（来自内部知识库，供参考。与问题无关时请忽略，"
                "不足的部分用你自己的知识补充并说明）\n" + "\n".join(ret_lines)
            )
        elif self.persona == "general":
            # 明确"没有资料也可以答"，否则模型会自己脑补一条"缺乏依据"的规则。
            volatile_parts.append(
                "[参考资料]（本次为空 —— 请直接用自己的知识回答，不要因此拒答）"
            )
        if tool_lines:
            volatile_parts.append("\n".join(tool_lines))
        volatile_text = "\n\n".join(volatile_parts)

        prefix_text = self.system_prompt
        if include_timestamp_in_prefix:
            prefix_text += f"\n[当前时间] {_t.strftime('%Y-%m-%d %H:%M:%S')}"

        messages: list[ChatMessage] = [system(prefix_text)]
        if not include_timestamp_in_prefix:
            messages.append(system(f"[当前时间] {_t.strftime('%Y-%m-%d %H:%M:%S')}"))
        messages.append(system(volatile_text))
        messages.append(user(user_input))

        total = sum(count_tokens(m.content) + 4 for m in messages)
        self.m_tokens.observe(total)
        self.m_dropped.inc(len(dropped))
        self.m_facts.set(facts_kept)
        return BuiltContext(
            messages=messages,
            prefix_text=prefix_text,
            volatile_text=volatile_text,
            tokens=total,
            kept_turns=len(recent),
            dropped_turns=len(dropped),
            summarized=bool(summary_text),
            facts_kept=facts_kept,
            stage_ms={"build": (_t.perf_counter() - t0) * 1000.0},
        )

    # -- 质量校验 -----------------------------------------------------------
    @staticmethod
    def key_fact_recall(original: str, compressed: str) -> float:
        """压缩后关键事实（数字/约束词/引用 id）的保留率。

        压缩不是免费的——这个数字就是它的价签。
        """
        def facts(text: str) -> set[str]:
            out: set[str] = set()
            token = ""
            for ch in text + " ":
                if ch.isdigit() or (token and ch in ".-%"):
                    token += ch
                else:
                    if token.strip(".-%"):
                        out.add(token.strip(".-%"))
                    token = ""
            for kw in ("必须", "禁止", "不得", "上限", "预算", "结论", "要求"):
                if kw in text:
                    out.add(kw)
            for part in text.split("[ref:"):
                if "]" in part:
                    out.add("ref:" + part.split("]", 1)[0])
            return out

        f0 = facts(original)
        if not f0:
            return 1.0
        return len(f0 & facts(compressed)) / len(f0)
