"""Public turn policies, results and coding prompt; no executor imports."""
from __future__ import annotations
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, Sequence
from agentlab.providers import ChatMessage
from agentlab.tracing import Tracer
from .agent_tools import AgentTool, build_agent_tools, schemas
from .compaction import Compactor
from .guard import CostGuardTripped
from .session import CheckpointError, SessionLog, replay
from .spill import DEFAULT_MAX_INLINE_BYTES, SpillPolicy
from .workspace import Workspace, WorkspaceError
from .model_client import ModelClient
@dataclass
class LoopContext:
    """喂给终止策略的实时状态。策略据此决定继续还是停。"""

    iteration: int
    tool_calls: int
    elapsed_s: float
    usd: float
    tokens_in: int
    tokens_out: int
    verified: bool           # 是否成功跑过验证命令
    last_tool: str = ""
    last_ok: bool = True
    finished: bool = False   # 模型是否已声明完成


class TurnPolicy(Protocol):
    """终止策略接口 —— 对应 DSH 的 `agent/turn-stopping` 钩子。

    返回 ``Stop`` 表示"停，并说明原因"；返回 ``None`` 表示"继续"。
    这是**唯一的策略注入点**，循环本身不假设任何轮次规则。
    """

    def __call__(self, ctx: LoopContext) -> "Stop | None": ...


@dataclass
class Stop:
    reason: str
    ok: bool = False


class MaxIterationsPolicy:
    """Only an explicitly configured model-step cap stops work; soft_limit is advisory.

    Lack of shell verification is not evidence of a loop (research/read-only tasks,
    unavailable execution environment, or a long implementation can all be valid).
    """
    def __init__(self, soft_limit: int = 40, hard_limit: int = 0):
        self.soft_limit = soft_limit
        self.hard_limit = hard_limit

    def __call__(self, ctx: LoopContext) -> Stop | None:
        if self.hard_limit and ctx.iteration >= self.hard_limit:
            return Stop(f"达到配置的模型调用上限 {self.hard_limit}；这不是死循环判定")
        return None


class BudgetPolicy:
    """按成本/时间设限。真实 key 下的常用策略。"""

    def __init__(self, max_usd: float | None = None, max_seconds: float | None = None):
        self.max_usd = max_usd
        self.max_seconds = max_seconds

    def __call__(self, ctx: LoopContext) -> Stop | None:
        if self.max_usd is not None and ctx.usd >= self.max_usd:
            return Stop(f"花费 ${ctx.usd:.4f} 已达策略上限 ${self.max_usd:.4f}")
        if self.max_seconds is not None and ctx.elapsed_s >= self.max_seconds:
            return Stop(f"耗时 {ctx.elapsed_s:.0f}s 已达策略上限 {self.max_seconds:.0f}s")
        return None


class CompositePolicy:
    """按顺序问多个策略，先给出停止意见的生效。"""

    def __init__(self, *policies: TurnPolicy):
        self.policies = [p for p in policies if p]

    def __call__(self, ctx: LoopContext) -> Stop | None:
        for p in self.policies:
            verdict = p(ctx)
            if verdict is not None:
                return verdict
        return None

# --------------------------------------------------------------------------
# 编码 agent 的系统提示
# --------------------------------------------------------------------------

from .system_prompts import AGENT_SYSTEM as CODING_SYSTEM


@dataclass
class LoopStep:
    """agent 循环里的一步。界面用它做实时回放。"""

    index: int
    kind: str                 # think | tool | observe | finish | error | guard
    title: str = ""
    detail: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)
    result: str = ""
    ok: bool = True
    ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0

    def to_dict(self) -> dict:
        return {
            "index": self.index, "kind": self.kind, "title": self.title,
            "detail": self.detail[:2000], "tool": self.tool, "args": self.args,
            "result": self.result[:4000], "ok": self.ok, "ms": round(self.ms, 1),
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "usd": round(self.usd, 6),
        }


@dataclass
class LoopResult:
    ok: bool
    summary: str = ""
    author_summary: str = ""
    acceptance: dict = field(default_factory=dict)
    steps: list[LoopStep] = field(default_factory=list)
    iterations: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    stopped_by: str = ""      # finish | finish_text_after_verification | policy | error
    error: str = ""
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    elapsed_ms: float = 0.0
    #: 反射闸门的结论（"证据闸门"/"需求清单"/"…（已达拒绝上限…）"）。
    #: 空串表示没有策略介入（只读任务，或反射被关掉）。
    #: **必须记下来**：否则"检查通过"和"没有检查"在结果上看起来一样。
    reflection: str = ""
    #: finish 被拒了几次。>0 说明模型第一次想蒙混过去。
    reflection_rejects: int = 0

    def render(self) -> None:
        from agentlab.util import kv, note, phase

        phase("Agent 循环回放", f"(模型调用 {self.model_calls} 次 / 工具调用 {self.tool_calls} 次)")
        for s in self.steps:
            icon = {"think": "💭", "tool": "🔧", "observe": "📄",
                    "finish": "🏁", "error": "❌", "guard": "🛡"}.get(s.kind, "·")
            line = f"  {icon} [{s.index:>2}] {s.title}"
            if s.ms:
                line += f"  ({s.ms:.0f}ms)"
            print(line)
            if s.detail:
                for ln in s.detail.splitlines()[:6]:
                    note(f"     {ln[:150]}")
        print()
        kv("结束原因", self.stopped_by)
        kv("总耗时", f"{self.elapsed_ms:.0f}ms")
        kv("token", f"in {self.tokens_in} / out {self.tokens_out}")
        kv("成本", f"${self.usd:.6f}")
