"""CodingAgent facade: construction and lifecycle composition.
Public imports remain compatible with previous agentplat.loop callers.
"""
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
from .loop_types import *
from .tool_protocol import _brief, _salvage_tool_args, _clean_path
from .review_lifecycle import ReviewLifecycle
from .conversation_runtime import ConversationRuntime
from .context_runtime import ContextRuntime
from .turn_runtime import TurnRuntime
from .model_runtime import ModelRuntime
from .tool_runtime import ToolRuntime

class CodingAgent(ReviewLifecycle, ConversationRuntime, ContextRuntime, ModelRuntime, ToolRuntime, TurnRuntime):
    """最小但完整的编码 agent。

    ``llm`` 需要提供 ``complete_with_tools(model, messages, tools, timeout_s)``。
    真实模型走 `OpenAIChatClient`；测试时可以注入假实现（见 tools/test_agent_loop.py），
    这样循环逻辑本身可以离线验证、不花钱。
    """

    #: 兜底软/硬上限。**不是主终止机制** —— 主机制是 `policy` 钩子。
    #: 数值来自实测：写"模块 + 测试 + 跑通 pytest"这类任务约需 25~30 轮，
    #: 软上限给到 12 会在"刚写完还没验证"处砍断（钱花了活儿没干完）。
    #: 见文件头关于"机制与策略分离"的说明。
    SOFT_ITERATIONS = 40
    HARD_ITERATIONS = 0  # 0 = no implicit model-step cap
    #: **单 step 内的工具调用上限**（对应 DSH 的 `maxParallelToolCalls`，但更严）。
    #:
    #: DSH 的 `maxParallelToolCalls: 10` 限的是**并发数**：10 个并发分成 5 批，
    #: 一轮里照样能跑 50 次调用。它的理由是"工具调用或 steering 会让当前轮次继续"，
    #: 即终止归策略管。但本项目实测（tools/test_agent_loop.py 用例④）：
    #: 一轮里塞 50 个调用的批处理会让单 step 直接失控，而**策略只在轮次边界检查**，
    #: 根本来不及介入 —— 那一轮 100 次调用全部执行完了。
    #:
    #: 所以在"单进程、无外层看门狗"的前提下，这里额外加一个 per-step 硬上限。
    #: 这是**有意的偏离**：DSH 有目标层与超时包兜底，本项目没有，就得自己兜。
    MAX_TOOLS_PER_STEP = 12

    def __init__(
        self,
        llm: ModelClient,
        cfg,
        workspace: Workspace | None = None,
        guard=None,
        on_step: Callable[[LoopStep], None] | None = None,
        policy: TurnPolicy | None = None,
        soft_iterations: int | None = None,
        hard_iterations: int | None = None,
        max_inline_bytes: int = DEFAULT_MAX_INLINE_BYTES,
        spill_enabled: bool = True,
        session_dir: Path | str | None = None,
        session_id: str | None = None,
        context_window: int | None = None,
        compaction_enabled: bool = True,
        stop_flag=None,
        invariants: bool = True,
        reflection: bool = True,
        max_wall_s: float | None = None,
        enable_subagents: bool = True,
        steering=None,
        services=None,
    ):
        self.llm = llm
        from .runtime_services import RuntimeServices
        self.services = services or RuntimeServices()
        from .tool_guards import ToolGuards
        self.tool_guards = ToolGuards()
        self.cfg = cfg
        self.ws = workspace or Workspace()
        self.guard = guard
        self.on_step = on_step
        self.steering = steering
        # 墙钟上限：**默认不设**（`None`）。
        #
        # 原来类属性里硬写着 `MAX_WALL_S = 900.0`，于是**长任务跑到 15 分钟
        # 就被静默掐断**，而且界面上既看不到、也改不了。用户的原话是
        # "轮数成本我都不在意，但不能影响其他的" —— 这条正是"影响其他的"：
        # 它不是成本控制，而是**凭空给任务设了一个看不见的截止时间**。
        #
        # 为什么可以不要它：真正防挂死的是**每一轮的模型超时 + 工具超时
        # + 成本护栏**，三层都在。墙钟只是"保险的保险"，
        # 而它的代价是砍掉正常的长任务（编译、跑测试套、大文件处理）。
        # 需要它的时候显式传 `max_wall_s=` 就行。
        self.max_wall_s = max_wall_s
        # 会话日志：每一步追加事件、屏障处 flush。
        # 没有它，进程一挂全部进度作废 —— 而一次编码任务实测要 $0.07~$0.38。
        #
        # ⚠ 每次运行必须有**独立的会话文件**。早期版本默认 session_id="auto"，
        # 结果所有运行都写进同一个 auto.jsonl：日志里堆着 10 个不同任务的
        # session/created，`find_latest_session()` 也就找不到"上次跑到哪"。
        # 会话日志是"一次运行一条时间线"，不能多个任务共用一份。
        if session_dir is None:
            session_dir = Path(self.ws.root).parent / ".sessions"
        if not session_id:
            session_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.session, _ = SessionLog.open(Path(session_dir) / f"{session_id}.jsonl",
                                         session_id=session_id)
        # 工具结果 spill：把超大输出挪出上下文，只留预览 + 落盘 locator。
        # 这是**上下文有界**的关键一环 —— 编码 agent 的 prompt 是被工具输出
        # 撑爆的，不是被用户输入撑爆的。
        self.spill = SpillPolicy(
            workspace=self.ws.root,
            max_inline_bytes=max_inline_bytes,
            enabled=spill_enabled,
        )
        # 上下文压缩：spill 管"单条结果过大"，compaction 管"轮数多了历史太长"。
        # 两者解决不同问题，都要有 —— 实测 45 轮时输入就到 348k token 了。
        self.compactor = Compactor(
            llm=llm, cfg=cfg, context_window=context_window if context_window is not None else cfg.resolved_context_window(),
            enabled=compaction_enabled,
        )
        self._explicit_context_window = context_window
        #: 外部中止开关（threading.Event）。**在步骤边界检查** ——
        #: 这样中止是"停在下一个安全点"，而不是把一次工具调用劈成两半
        #: （写文件写到一半被杀 = 文件损坏，比继续跑完更糟）。
        self.stop_flag = stop_flag
        if stop_flag is not None:
            self.ws.cancel_event = stop_flag
        # 终止策略：钩子优先；没有就给一个保守兜底。
        #
        # ⚠ `hard_iterations` 的语义是**整个循环的硬上限**，不是策略的。
        # 原来只把它传给 `MaxIterationsPolicy`，而 `while iteration <
        # self.HARD_ITERATIONS` 用的是类属性 80 —— 于是：
        #   · 传 24 → 策略在 24 停（比设计的 40 还早）；
        #   · 传 200 → 策略放到 200，但 while 仍在 80 停，**参数被静默忽略**。
        # 两个上限不一致会让"我明明调大了却没生效"变成一个查不出来的谜。
        # 现在统一到 `self.hard_iterations`，只留一个数。
        self.hard_iterations = self.HARD_ITERATIONS if hard_iterations is None else hard_iterations
        self.soft_iterations = self.SOFT_ITERATIONS if soft_iterations is None else soft_iterations
        if self.hard_iterations < 0 or self.soft_iterations < 1:
            raise ValueError("模型调用上限必须非负，0 表示不限；提示阈值必须为正")
        self.policy = CompositePolicy(
            policy or (lambda _ctx: None),
            MaxIterationsPolicy(
                soft_limit=self.soft_iterations,
                hard_limit=self.hard_iterations,
            ),
        )
        from .runtime import CapabilityPolicy, Evidence, TaskMemory, workspace_digest
        self.capabilities = CapabilityPolicy()
        self.evidence = Evidence()
        self.task_memory = TaskMemory()
        self._initial_digest = workspace_digest(self.ws.scope)
        self.tools: dict[str, AgentTool] = build_agent_tools(self.ws)
        from .knowledge import install_knowledge_tools
        install_knowledge_tools(self)
        from .attachments import install as install_attachments
        install_attachments(self)
        from .memory import install as install_memory
        install_memory(self)
        self.children = None
        from .human_input import install as install_human_input
        install_human_input(self)
        if enable_subagents:
            from .extended_tools import install_runtime_tools
            install_runtime_tools(self)
        from .plugins import install as install_plugins
        install_plugins(self)

        # ---- 运行时不变量（对齐 DSH `dsh-invariants`）----
        # 挂在会话日志的观察者上：每个事件落盘后过一遍全称检查。
        # 测试是抽样的（45 轮走出 10^40 种路径，只能测几条），
        # 不变量是全称的（"每个 tool/call 都有配对 result"要么成立要么立刻报错）。
        self.invariants = None
        #: 多轮对话的载体：`run()` 时建立，`continue_with()` 时接续。
        #: 没有它，每次提问对模型都是全新的对话（详见 continue_with 的 docstring）。
        self._conversation: list[ChatMessage] = []
        # -- 反射状态（见 reflection.py）------------------------------------
        #: 本次任务原文。需求清单要从它抽条目 —— 从 messages 里反推是不行的，
        #: 因为压缩可能已经把那条 user 消息改成摘要了。
        self._task_text = ""
        #: 本轮**真的**改动过的文件（来自 workspace 的审计，不是模型自述）。
        #: 用自述会有个循环论证：模型说改了、模型说验证过了 —— 两句话都来自
        #: 同一个可能出错的地方。
        self._files_touched: list[str] = []
        #: 验证失败过几次。"验证过但又改坏了"是个真实信号。
        self._failed_verifies = 0
        #: finish 被反射闸门拒了几次（有上限，见 reflection.MAX_REJECTS）
        self._finish_rejects = 0
        self.reflector = None
        if reflection:
            from .reflection import default_reflector

            self.reflector = default_reflector()
        #: 全局轮次基准：第 `n` 轮记成 `_iter_offset + n - 1`。
        #: 初始为 1（首轮记 1..N）；一轮跑完 `_iter_offset += res.iterations`，
        #: 于是多轮对话的轮次编号**跨轮连续且严格递增**。
        #: 每轮重新从 1 开始会让日志出现"轮次回退"，而"第几轮出的问题"这个
        #: 归因就废了 —— 实测就是被不变量抓出来的。
        self._iter_offset = 1
        self._used_call_ids = set()
        self._persisted_messages = []
        #: 最近一次构建出来的 messages。压缩不变量要靠它做"记账 vs 实际"的比对
        #: （报告说剪了 32 条，历史里就必须真有 32 条占位符）。
        #: 没有注册表时保持 None，读它的检查会自动跳过。
        self._last_messages: list | None = None
        if invariants:
            from . import compaction_invariant
            from .invariants import build_default_registry
            from .loop_invariant import attach_ledger

            self.invariants = build_default_registry()
            compaction_invariant.attach_messages(
                self.invariants, lambda: self._last_messages)
            if guard is not None:
                # 成本闭合是**跨模块**的检查：会话日志的总额 vs 账本。
                # 用注入而不是 import guard，这样没账本的场景也跑得起来。
                attach_ledger(self.invariants,
                              lambda: getattr(guard, "spent_usd", 0.0))
            self.session.observers.append(self._on_session_event)


    def _on_session_event(self, ev) -> None:
        """把落盘后的事件喂给不变量检查。"""
        if self.invariants is not None:
            self.invariants.dispatch(ev)


    def _close_tasks(self):
        if getattr(self, 'browser', None):
            self.browser.close()
        if self.children:
            self.children.close()
        self.ws.processes.close()


def main(argv=None):
    from .agent_cli import main as cli_main
    return cli_main(argv)

if __name__=="__main__":
    raise SystemExit(main())
